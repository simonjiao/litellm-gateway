# 部署设计

## 平台能力

部署环境必须具备：

- OCI 工作负载及按工作负载选择 `runc`/`runsc` 的能力；
- 可授权给 Sandbox Manager 的 Worker、Workspace 卷和一次性任务控制接口；
- 工作负载 DNS；
- 支持方向和连接状态的网络访问控制；
- Secret、持久卷、健康检查和资源限额；
- Manager 控制状态的持久化和重启对账能力；
- Open WebUI Files 可访问的私有对象存储；
- control 网络域的默认外网，以及 Agent 的默认拒绝外网。

这些要求不限定 Docker、Kubernetes 或其他编排实现。

## 工作负载配置

| 工作负载 | Runtime | 网络域 | 特殊权限 |
|---|---|---|---|
| Open WebUI / 同源 BFF | `runc` | control、storage | 文件 ACL 与操作授权 |
| LiteLLM Gateway | `runc` | control | 无 |
| Responses Adapter | `runc` | control、agent-rpc | 无 |
| Sandbox Manager | `runc` | control、storage | 单实例；SQLite 状态卷；受管 Worker、Workspace 卷和一次性任务权限 |
| Sandbox Worker | `runsc` | agent-rpc、agent-egress | 独立工作区与 Runtime Secret |
| 受控一次性任务 | `runc` | storage | 单 Workspace 挂载与单次操作授权 |
| agent-dns | `runc` | agent-egress | 无 |
| egress-proxy | `runc` | agent-egress、egress-uplink | 无 |

一次性任务按 checkout、publish、checkpoint、restore 或 retire 动态创建，不是常驻传输服务。
Open WebUI 暴露用户界面，Gateway 暴露 Responses 入口。BFF 可作为 Open WebUI 的同源后端
扩展，不要求独立公共服务。Adapter、Manager、Worker 和一次性任务不暴露公共端口。

## 服务发现

配置只使用 DNS 服务名，不固定 IP。下面的名称和端口均为部署时分配的逻辑标识，不是
架构常量：

```text
Open WebUI → gateway.<control-domain>:<gateway-port>
Gateway  → adapter.<control-domain>:<responses-port>
BFF      → adapter.<control-domain>:<apps-port>
Adapter  → sandbox-manager.<control-domain>:<control-port>
Adapter  → sandbox-worker-<execution-id>.<rpc-domain>:<worker-rpc-port>
Worker   → egress-proxy.<egress-domain>:<proxy-port>
one-shot → files.<storage-domain>:<files-port> | object-store.<storage-domain>:<storage-port>
```

Manager 为每个 Worker 分配不可预测的服务名和独立凭证，并在健康检查通过后返回给 Adapter。
Agent DNS 只解析策略代理和明确允许的内部接口。

## 网络策略

部署环境必须实现：

```text
allow  Gateway/BFF → Adapter
allow  Adapter → Sandbox Manager
allow  Adapter → Worker RPC/SSE NEW
allow  Worker → agent-dns:53 UDP/TCP
allow  Worker → approved internal interfaces
allow  ESTABLISHED,RELATED
deny   Worker → Adapter/Manager NEW
deny   Worker → Files/object-storage NEW
deny   Worker → Internet DIRECT
allow  Worker → egress-proxy
allow  one-shot → operation-scoped Files/object-storage endpoint
```

规则应基于工作负载身份、标签、网络域或运行时发现的数据生成，不依赖固定 IP。Adapter 的
服务监听面只属于 control；agent-rpc 仅用于 Adapter 发起到 Worker 的连接。

Docker 参考部署只接受本地 IPv4 bridge 网络。进入 agent-rpc、agent-egress 网桥的全部转发
流量均进入默认拒绝策略，发往宿主的流量由 INPUT 策略拒绝。规则先写入未引用链，完整后再
切换生效链；地址或服务校验失败时保留原有策略。

egress-proxy 是 agent-egress 唯一外网出口。MCP Gateway 或本地模型需要被 Agent 访问时，
显式加入 agent-egress，并按服务身份、DNS 名称和端口放行。agent-egress 的成员变更属于运行
平台授权操作：只有 Manager 可附加受管 Worker，部署方可附加声明的基础设施和内部服务；
策略应用时拒绝未声明成员。网络准入不能替代内部服务自身的 Bearer 或 mTLS 认证。

## 权限与 Secret

- Manager 只获得管理受管 Worker、Workspace 卷和一次性任务所需的运行平台权限；不持有
  Open WebUI Files 或业务 ACL 权限。
- Adapter 不持有运行平台控制凭证。
- Worker 不持有 Adapter、Manager、Open WebUI、对象存储或运行平台凭证。
- 一次性任务最多挂载一个 Workspace，并只持有当前操作的短期、最小权限凭证。
- Gateway、Manager、Adapter 和 Worker 使用不同的部署凭证。
- BFF 与 Manager 共享独立的操作签名 Secret；Adapter 只能转交签名令牌。
- Open WebUI Files 与 Workspace restic 仓库使用不同的 S3 凭证；restic repository password
  使用独立 Secret。
- Manager 的 RustFS 父凭证仅限 Workspace repository 范围，用 STS 为单次任务签发
  15–60 分钟的 prefix/action 限定会话；任务不持有父凭证。
- Agent Runtime 认证与配置以只读 Secret 注入 Worker；Workspace 与 Runtime 状态目录分离。
- 底层平台接口权限过大时，Manager 应运行在专用节点或等价隔离域。

## Sandbox Worker 基线

每个 Worker 必须设置：

- `runtime=runsc`；
- 非 root、只读根文件系统、cap-drop all、`no-new-privileges`；
- CPU、内存、PID、临时文件系统和执行 TTL 限额；
- 独立工作区、Runtime 状态目录和只读部署 Secret；
- `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 指向 egress-proxy；
- Agent Runtime 接受外层 Sandbox 边界，不创建不兼容的内层 Sandbox；审批和权限提升 fail closed。

Manager 返回 Sandbox/Workspace/operation 控制状态和 Worker 连接信息，不返回文件内容。
Agent RPC、事件流及 Runtime 反向请求由 Adapter 与 Worker 直接处理。运行中的 Agent 执行和
MCP App 交互续租；终态 Response 不保持无限租约。Sandbox 过期后，`previous_response_id`
和 AppSession 调用返回 `sandbox_unavailable`。详细边界见
[Sandbox Manager 设计](sandbox-manager.md)。

## 环境映射

| 平台能力 | 独立 Docker 环境示例 | Kubernetes 环境示例 |
|---|---|---|
| Agent runtime | Docker `runsc` runtime | `RuntimeClass: runsc` |
| 运行平台控制 | 受隔离的 Engine 接口 | 限定命名空间的 ServiceAccount/RBAC |
| 服务发现 | 用户定义网络 DNS | Service/Pod DNS |
| 单向访问控制 | 主机防火墙或等价策略 | CNI NetworkPolicy |
| Secret/存储 | Secret 文件与命名卷 | Secret 与 PVC |
| Workspace 快照 | restic + 私有对象存储 | restic Job + 私有对象存储 |

环境映射只说明满足接口要求的方式，不改变上面的权限和网络不变量。

租约与最长执行时间、单 Workspace 容量与文件数、并发操作数、checkpoint 超时、
本地卷保留期、远端 revision 保留期和对话删除宽限期均为部署参数，不写死在业务
逻辑中。超限操作在创建任务前拒绝。

## 实现方案与规模

Open WebUI 使用以 v0.11.1 为基线的派生镜像，只增加同源 BFF 路由和最小消息附件集成，并直接
复用上游认证、Chats、Files 与 Storage。Manager 使用标准库 SQLite WAL；checkout/publish
使用同一个轻量一次性任务镜像，checkpoint/restore 直接调用镜像内的 restic。宿主机不安装
restic，RustFS STS 负责临时 S3 凭证，也不增加常驻传输服务。现有
`DockerSandboxBackend` 保留为运行平台实现，扩展卷、标签和一次性任务管理。

以下为本次需新增或重写的手写逻辑估算，不含现有未改代码、Open WebUI 上游代码、
restic、生成文件和依赖锁文件：

| 分类 | 实现内容 | 生产代码 | 测试代码 |
|---|---|---:|---:|
| Open WebUI/BFF | 对话映射、ACL、签名授权、受控 Files 传输和消息附件 | 350–550 行 | 250–400 行 |
| Adapter | 内部 Workspace 授权转交、敏感 metadata 剥离和操作调用 | 100–180 行 | 100–160 行 |
| Manager 状态与接口 | SQLite schema/repository、模型、事务、nonce、STS 和控制接口 | 350–550 行 | 300–450 行 |
| Manager Docker 控制 | 卷与 Sandbox 解耦、单写者、对账、reaper 和一次性任务编排 | 500–750 行 | 450–650 行 |
| 一次性任务 | 安全 checkout/publish 客户端、restic 调用和结果清单 | 200–350 行 | 150–250 行 |
| 部署与验收 | 镜像、Compose、Secret、storage 网络、脚本和 smoke | 150–250 行 | 150–250 行 |
| 合计 | — | 1,650–2,630 行 | 1,400–2,160 行 |

总实现规模预计为 3,050–4,790 行，其中约一半用于隔离、失败恢复和验收。实施顺序固定为：
Manager 持久状态与 Workspace 卷、checkpoint/restore、Open WebUI/BFF 与 Adapter 绑定、
checkout/publish、端到端部署验收。

## Docker 参考部署

仓库中的 `compose.yaml` 将 Open WebUI、Gateway、Adapter 和 Sandbox Manager 作为独立
`runc` 服务部署；Open WebUI 从固定上游基线构建派生镜像，Gateway、Adapter、Manager、
Sandbox Worker 和一次性任务分别使用独立镜像。公共 Compose 配置只复用运行时加固项。
Manager 通过 Docker socket 创建 `runsc` Worker；Compose 通过
`SANDBOX_MANAGER_DOCKER_SOCKET` 注入本地 Engine 或授权代理的 Unix socket。该接口不能限制
对象范围时，应将参考部署置于专用 Docker Engine 或专用节点。`run-stack.sh` 负责构建镜像、
准备四个逻辑网络、启动 DNS、策略代理和内部服务，应用方向性规则后才启动 Gateway：

默认镜像为 `agent-open-webui:0.3.0`、`agent-gateway:0.3.0`、`agent-adapter:0.3.0`、
`agent-sandbox-manager:0.3.0`、`codex-sandbox-worker:0.3.0` 和一次性任务镜像
`agent-storage-ops:0.3.0`；分别通过 `AGENT_OPEN_WEBUI_IMAGE`、`AGENT_GATEWAY_IMAGE`、
`AGENT_ADAPTER_IMAGE`、`AGENT_SANDBOX_MANAGER_IMAGE`、`SANDBOX_IMAGE` 和
`AGENT_STORAGE_OPS_IMAGE` 覆盖。

`agent-open-webui` 以 `ghcr.io/open-webui/open-webui:v0.11.1` 为固定基线。Open WebUI 默认发布到
宿主端口 `3000`，数据保存在命名卷 `open-webui-data`。它将
`http://gateway:4000/v1` 配置为 Responses 连接，不设置连接级 `model_ids`，因此模型选择器
直接使用 Gateway 发布的目录。`config/litellm.yaml` 中的参考目录为：

| 公共模型 ID | Gateway 路由 |
|---|---|
| `codex-sol` | `openai/gpt-5.6-sol` |
| `codex-terra` | `openai/gpt-5.6-terra` |
| `codex-luna` | `openai/gpt-5.6-luna` |

`model_name` 是公共模型 ID，`litellm_params.model` 是 Gateway 路由。

Open WebUI 的部署配置为：

```yaml
OPENAI_API_CONFIGS: '{"0":{"api_type":"responses"}}'
DEFAULT_MODELS: codex-terra
```

用户在聊天输入区的模型选择器切换模型，无需修改前端。

启用 `AGENT_WORKSPACE_ENABLED` 和 `SANDBOX_MANAGER_STORAGE_ENABLED` 后，同源 BFF 为对话创建
Workspace，上传附件在 Agent 执行前 checkout；`POST /api/agent/artifacts/publish` 将指定
Workspace 普通文件附加到对应助手消息，并返回 Open WebUI 鉴权下载链接。RustFS 连接、Files
凭证和 Workspace STS 父凭证见 `.env.example`；`run-stack.sh` 首次启动时生成独立 restic
repository password，后续启动复用。
本地已有 rclone 业务凭证时，`scripts/configure-rustfs.py --remote rustfs` 将其导入 `.env` 并使用
`static` 模式；生产环境默认使用支持 `AssumeRole` 的 `sts` 模式。

首次注册用户成为管理员，之后公开注册关闭。Open WebUI 内置工具注入默认关闭；Agent 工具仍由
Codex 和 MCP Gateway 管理。`OPEN_WEBUI_SECRET_KEY` 必须是独立、稳定的随机 Secret。

网络配套镜像默认为 `agent-egress-proxy:0.1.0`、`agent-dns:0.1.0` 和
`agent-network-policy:0.1.0`；分别通过 `AGENT_EGRESS_PROXY_IMAGE`、`AGENT_DNS_IMAGE` 和
`AGENT_NETWORK_POLICY_IMAGE` 覆盖。

```bash
cp .env.example .env
bash scripts/run-stack.sh
```

该参考部署提供标准 Responses 聊天入口。MCP Apps 部署将 `frontend/mcp-apps-host` 接入
Open WebUI 消息渲染，并由同源 BFF 代理 Adapter 的 `/v1/mcp-apps/*` 接口。

默认复用 `$CODEX_HOME/auth.json`（未设置时为 `$HOME/.codex/auth.json`）。脚本将认证副本放入
忽略版本控制且权限为 `0700` 的 Secret 根目录，再只读注入 Worker；不会把认证写入镜像。
自定义 `SANDBOX_MANAGER_SECRET_ROOT` 也必须由部署用户拥有且权限为 `0700`。

Open WebUI、Gateway 和 Adapter 不绕过宿主策略自动重启。Docker 或宿主重启、
Adapter/DNS/代理/内部服务变化后，必须再次执行 `run-stack.sh`；脚本重建 Manager 和
Adapter、清理现有 Worker 及实例级临时 Workspace、原子恢复策略，失败时保持 Open WebUI、
Gateway 和 Adapter 停止。部署操作会结束活动 Sandbox，但必须保留 Manager 状态卷和可恢复
Workspace，并由 Manager 重启后对账。

## 验收

- 普通工作负载可通过服务名互通，并按部署策略访问外网。
- Adapter 可通过 Worker 服务名完成 RPC/SSE。
- Worker 无法向 Adapter 或 Manager 发起连接。
- Worker 无法直接访问互联网，只能通过 egress-proxy 访问允许目标。
- Manager 无 Agent 数据面转发接口。
- Sandbox 内 Agent Runtime 能够执行工作区命令，且不启动与外层隔离不兼容的内层 Sandbox。
- Gateway 模型接口只返回已发布的公共 ID，Open WebUI 显示相同目录；未发布模型在创建
  Sandbox 前被拒绝。
- 每个公共模型 ID 均路由到对应 Agent Runtime 模型，对外 Response 保持公共 ID。
- 缺少授权、过期、重放或绑定到其他 Sandbox/Workspace 的文件操作全部 fail closed。
- Worker 无法访问 Files 或对象存储；一次性任务最多访问一个 Workspace 和当前操作端点。
- checkpoint 成功并提交 revision 后才能延迟删除本地卷；失败时保留 dirty 本地卷。
- restore 在新空卷中完成校验后才启动 Worker，恢复内容与指定 revision 一致。
- 已发布生成物在 Sandbox/Workspace 清理后仍可经 Open WebUI ACL 链接下载。
- Manager 重启后可从持久记录对账 Workspace、受管任务和未完成操作。

以下脚本覆盖网络边界和基本 Responses 执行链路：

```bash
bash scripts/check-egress-policy.sh
bash scripts/run-basic-smoke.sh
```

第一项同时验证代理白名单、Adapter→Worker DNS/TCP 方向性，以及 Worker 无法访问 Adapter、
Manager、Gateway、宿主网桥和运行时解析得到的公网 IP。第二项经 Gateway、Adapter 和真实
Worker 要求 Agent 在工作区执行随机 nonce 的 shell 摘要命令，并校验返回值。
