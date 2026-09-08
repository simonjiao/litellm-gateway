# Agent 出站策略

## 边界

Sandbox Worker 同时加入内部网络 `agent-rpc` 和 `agent-egress`。前者只承载
Adapter→Worker RPC/SSE；后者只允许 Worker 访问 `agent-dns`、`egress-proxy` 和明确声明的
内部服务。两个网络均不提供直接互联网路由。DNS、代理在 `agent-egress` 上、Adapter 在
`agent-rpc` 上使用固定地址；Worker 从各网络独立的动态地址池分配地址。

```text
Adapter ── agent-rpc ──→ Worker (runsc)
                            │
                            └── agent-egress
                                  ├── agent-dns:53
                                  ├── egress-proxy:3128 ── egress-uplink ── Internet
                                  └── approved service:declared-port
```

`runsc` Worker 使用部署生成的只读 resolver 文件，不依赖 Docker 注入的
`127.0.0.11`。`agent-dns` 没有上游，只解析策略代理和
`SANDBOX_AGENT_INTERNAL_SERVICES` 声明的精确服务名。部署配置统一指定固定网段、网关、动态池
和服务地址；默认 DNS 为 `172.22.0.53`、代理为 `172.22.0.54`、Adapter RPC 为 `172.21.0.10`。
两个动态池分别为 `172.22.128.0/17` 和 `172.21.128.0/17`，不会自动占用固定地址。
启动脚本校验实际服务地址后生成 hosts 和 resolver；内容不变时保留 resolver 文件，避免
已有只读文件挂载失效。固定基础服务重建沿用原地址，无需运行时更新 DNS。
重建可能暂时中断现有连接，Worker 通过重试恢复。

`prepare-sandbox-network.sh` 拒绝与配置不符的现有网络，不自动删除网络或容器。旧部署首次
迁移时需停止入口、Manager 和 Worker，移除受影响容器，再重建 `agent-rpc`、`agent-egress`。
保留 Manager 状态卷、可恢复 Workspace 和 Artifact 数据；后续正常重建不需要重建网络。
额外声明的内部服务应固定其 Agent 网络地址；修改这些服务的地址或声明后需重新部署。

主机策略运行时发现网络网桥和服务地址。`DOCKER-USER` 检查进入 Agent 网桥的全部转发流量，
INPUT 链拒绝 Agent 到宿主的连接，只允许：

- Adapter 向 Worker TCP/8091 发起新连接；
- Worker 向 agent-dns TCP/UDP 53、egress-proxy TCP/3128 发起新连接；
- Worker 向显式内部服务的声明端口发起新连接；
- 已建立连接的返回流量。

DNS、代理和已声明内部服务只能返回已建立连接，不能主动使用代理或访问其他声明服务。其他
Agent 流量默认拒绝。网络成员由 Manager 或部署方准入，策略应用时拒绝非受管 Worker 和
未声明服务。规则通过未引用链构建并原子切换；`run-stack.sh` 在规则成功前保持 Gateway
停止，Adapter 不发布宿主端口，失败时同时停止二者。

## 公网策略

基础 smoke 的外部精确域名位于
[`deploy/egress-proxy/allowed-domains.txt`](../deploy/egress-proxy/allowed-domains.txt)：

- `chatgpt.com`：Codex 模型后端；
- `auth.openai.com`：ChatGPT 登录令牌刷新。

Squid 只允许来自 agent-egress、目标端口为 443 的 HTTPS `CONNECT`，先拒绝私网和链路本地
目标，再按精确域名放行，最后默认拒绝。代理不解密 TLS，也不缓存内容，不发布宿主机端口。
若宿主必须经过 HTTP 上游代理，设置
`SANDBOX_EGRESS_UPSTREAM_PROXY_URL=http://host:port`；回环地址通过只绑定 uplink 网桥的 relay
转发，Worker 仍不能直接访问上游。

## 内部服务

MCP Gateway 或本地模型容器先显式加入 `agent-egress`，再配置：

```dotenv
SANDBOX_AGENT_INTERNAL_SERVICES=mcp-gateway=mcp-gateway:8443,local-model=local-model:8080
```

每项同时生成精确 DNS 记录、Worker `NO_PROXY` 名称和目标 TCP 端口规则。内部服务仍须使用独立
Bearer Token 或 mTLS；加入网络但未声明的服务不可访问。

## 启动与验收

```bash
cp .env.example .env
bash scripts/run-stack.sh
bash scripts/check-egress-policy.sh
bash scripts/run-basic-smoke.sh
```

镜像已准备好时使用 `bash scripts/run-stack.sh --no-build`。启动流程会先恢复方向性规则并
完成真实 `runsc` 连通性检查，再启动 Gateway 和 Open WebUI；检查失败时保持入口停止。
DNS、代理由 `unless-stopped` 在进程退出后重启；手动停止或删除后需显式启动或重建。

策略检查使用真实 `runsc` Worker 镜像，验证允许域名可经代理访问、未允许域名被拒绝，并验证
Adapter 可通过服务名连接 Worker，而 Worker 不能连接控制面、宿主网桥或运行时解析得到的
公网 IP。测试地址只用于当次探测，不写入部署配置。
