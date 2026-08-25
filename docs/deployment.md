# 部署设计

## 平台能力

部署环境必须具备：

- OCI 工作负载及按工作负载选择 `runc`/`runsc` 的能力；
- 可授权给 Sandbox Manager 的工作负载生命周期接口；
- 工作负载 DNS；
- 支持方向和连接状态的网络访问控制；
- Secret、持久卷、健康检查和资源限额；
- control 网络域的默认外网，以及 Agent 的默认拒绝外网。

这些要求不限定 Docker、Kubernetes 或其他编排实现。

## 工作负载配置

| 工作负载 | Runtime | 网络域 | 特殊权限 |
|---|---|---|---|
| LiteLLM Gateway | `runc` | control | 无 |
| Responses Adapter | `runc` | control、agent-rpc | 无 |
| Sandbox Manager | `runc` | control | Sandbox 工作负载生命周期权限 |
| Sandbox Worker | `runsc` | agent-rpc、agent-egress | 独立工作区与 Runtime Secret |
| agent-dns | `runc` | agent-egress | 无 |
| egress-proxy | `runc` | agent-egress、egress-uplink | 无 |

只有 Gateway 暴露 Responses 入口。BFF 如需代理 MCP Apps，加入 control 或通过等价私网
访问 Adapter。Adapter、Manager 和 Worker 不暴露公共端口。

## 服务发现

配置只使用 DNS 服务名，不固定 IP。下面的名称和端口均为部署时分配的逻辑标识，不是
架构常量：

```text
Gateway  → responses-adapter.<control-domain>:<responses-port>
BFF      → responses-adapter.<control-domain>:<apps-port>
Adapter  → sandbox-manager.<control-domain>:<lifecycle-port>
Adapter  → sandbox-worker-<execution-id>.<rpc-domain>:<worker-rpc-port>
Worker   → egress-proxy.<egress-domain>:<proxy-port>
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
deny   Worker → Internet DIRECT
allow  Worker → egress-proxy
```

规则应基于工作负载身份、标签、网络域或运行时发现的数据生成，不依赖固定 IP。Adapter 的
服务监听面只属于 control；agent-rpc 仅用于 Adapter 发起到 Worker 的连接。

Docker 参考部署只接受本地 IPv4 bridge 网络。进入 agent-rpc、agent-egress 网桥的全部转发
流量均进入默认拒绝策略，发往宿主的流量由 INPUT 策略拒绝。规则先写入未引用链，完整后再
切换生效链；地址或服务校验失败时保留原有策略。

egress-proxy 是 agent-egress 唯一外网出口。MCP Gateway 或本地模型需要被 Agent 访问时，
显式加入 agent-egress，并按服务身份、DNS 名称和端口放行；加入网络本身不构成授权。

## 权限与 Secret

- Manager 只获得创建、查询、续租和销毁 Sandbox Worker 所需权限。
- Adapter 不持有运行平台控制凭证。
- Worker 不持有 Adapter、Manager 或运行平台凭证。
- Gateway、Manager、Adapter 和 Worker 使用不同的部署凭证。
- Agent Runtime 认证与配置以只读 Secret 注入 Worker；工作区和 Runtime 状态目录使用独立持久卷。
- 底层平台接口权限过大时，Manager 应运行在专用节点或等价隔离域。

## Sandbox Worker 基线

每个 Worker 必须设置：

- `runtime=runsc`；
- 非 root、只读根文件系统、cap-drop all、`no-new-privileges`；
- CPU、内存、PID、临时文件系统和执行 TTL 限额；
- 独立工作区、Runtime 状态目录和只读部署 Secret；
- `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 指向 egress-proxy；
- Agent Runtime 接受外层 Sandbox 边界，不创建不兼容的内层 Sandbox；审批和权限提升 fail closed。

Manager 只返回生命周期状态和 Worker 连接信息。Agent RPC、事件流及 Runtime 反向请求
由 Adapter 与 Worker 直接处理。运行中的 Agent 执行和 MCP App 交互续租；终态
Response 不保持无限租约。Sandbox 过期后，`previous_response_id` 和 AppSession 调用返回
`sandbox_unavailable`。

## 环境映射

| 平台能力 | 独立 Docker 环境示例 | Kubernetes 环境示例 |
|---|---|---|
| Agent runtime | Docker `runsc` runtime | `RuntimeClass: runsc` |
| 生命周期权限 | 受隔离的 Engine 接口 | 限定命名空间的 ServiceAccount/RBAC |
| 服务发现 | 用户定义网络 DNS | Service/Pod DNS |
| 单向访问控制 | 主机防火墙或等价策略 | CNI NetworkPolicy |
| Secret/存储 | Secret 文件与命名卷 | Secret 与 PVC |

环境映射只说明满足接口要求的方式，不改变上面的权限和网络不变量。

## Docker 参考部署

仓库中的 `compose.yaml` 将 Gateway、Adapter 和 Sandbox Manager 作为独立 `runc` 服务部署，
只发布 Gateway 端口。Manager 通过 Docker socket 创建 `runsc` Worker；当前 Compose 通过
`SANDBOX_MANAGER_DOCKER_SOCKET` 注入本地 Engine 或授权代理的 Unix socket。该接口不能限制
对象范围时，应将参考部署置于专用 Docker Engine 或专用节点。`run-stack.sh` 负责构建镜像、
准备三个逻辑网络、启动 DNS/策略代理和内部控制面，应用方向性规则后才启动 Gateway：

```bash
cp .env.example .env
bash scripts/run-stack.sh
```

默认复用 `$CODEX_HOME/auth.json`（未设置时为 `$HOME/.codex/auth.json`）。脚本将认证副本放入
忽略版本控制且权限为 `0700` 的 Secret 根目录，再只读注入 Worker；不会把认证写入镜像。
自定义 `SANDBOX_MANAGER_SECRET_ROOT` 也必须由部署用户拥有且权限为 `0700`。

Gateway 和 Adapter 不绕过宿主策略自动重启。Docker 或宿主重启、Adapter/DNS/代理/内部服务
变化后，必须再次执行 `run-stack.sh`；脚本重建 Manager 和 Adapter、清理现有 Worker、原子
恢复策略，失败时保持 Gateway 和 Adapter 停止。因此部署操作会结束活动 Sandbox，并清除
Adapter 的单进程内存状态。

## 验收

- 普通工作负载可通过服务名互通，并按部署策略访问外网。
- Adapter 可通过 Worker 服务名完成 RPC/SSE。
- Worker 无法向 Adapter 或 Manager 发起连接。
- Worker 无法直接访问互联网，只能通过 egress-proxy 访问允许目标。
- Manager 无 Agent 数据面转发接口。
- Sandbox 内 Agent Runtime 能够执行工作区命令，且不启动与外层隔离不兼容的内层 Sandbox。

Docker 参考部署执行：

```bash
bash scripts/check-egress-policy.sh
bash scripts/run-basic-smoke.sh
```

第一项同时验证代理白名单、Adapter→Worker DNS/RPC，以及 Worker 无法访问 Adapter、Manager、
Gateway、宿主网桥和运行时解析得到的公网 IP。第二项要求 Agent 在工作区执行随机 nonce 的
shell 摘要命令，并校验返回值。
