# Sandbox Manager 设计

本文细化 [总体设计](design.md) 中的 Sandbox Manager。这里的能力分组是一个服务内部的接口和
权限边界，不要求拆成多个微服务。

## 定位与边界

Manager 是可信执行控制面，负责把“运行哪个 Sandbox、挂载哪个 Workspace、执行哪一个受控
数据操作”落实到运行平台。它不处理 Responses、Agent RPC/SSE 或文件字节，也不判断用户和
对话的业务权限。

| 标识 | 含义 |
|---|---|
| `sandbox_id` | 一次可销毁的计算实例 |
| `workspace_id` | 独立于计算实例的 Workspace 身份，可配置为临时或可恢复 |
| `revision_id` | 已提交到远端仓库的不可变 Workspace 快照 |
| `operation_id` | checkout、publish、checkpoint 或 restore 的幂等操作 |

一个可恢复 Workspace 同时最多有一个可写 Worker。Manager 使用 Workspace 锁和本地代次阻止
旧 Worker 或重复任务继续写入。

## Sandbox 生命周期

Manager 提供创建、查询、续租和销毁能力：

1. 校验调用方和请求；若给出 `workspace_id` 则验证并挂载，未给出则创建实例级临时 Workspace；
   生成不可预测的 `sandbox_id` 与独立连接凭证。
2. 创建指定资源限制、`runsc` runtime、网络域、Secret 和 Workspace 挂载的 Worker。
3. 健康检查成功后向 Adapter 返回 Worker 地址、凭证和租约期限；不转发后续 Agent 流量。
4. 续租时同时验证 Sandbox 状态和最大执行期限；过期、取消或异常时回收 Worker。
5. Manager 重启后按受管标签与持久记录对账，回收孤儿 Worker，保留无法确认已备份的卷。

销毁 `sandbox_id` 只结束计算。临时 Workspace 可随之删除；可恢复 Workspace 必须进入下面的
detach/checkpoint 流程。

## Workspace 生命周期

Workspace 记录至少包含类型、状态、本地卷与代次、远端 head revision、当前 Sandbox、租约、
操作和保留期限。控制状态保存在 Manager 持久数据库，文件内容不进入该数据库。

```text
RUNNING
  → DETACHED_DIRTY
  → CHECKPOINTING
  → DETACHED_CLEAN
  → REMOTE_ONLY

REMOTE_ONLY / DETACHED_CLEAN
  → RESTORING
  → RUNNING
```

- `detach`：停止或确认已停止写入，卸载 Worker，标记本地数据为 dirty。
- `checkpoint`：一次性 `workspace-checkpoint-*` 任务只读挂载该卷，使用 restic 创建增量快照。
- `commit`：快照和校验完成后，Manager 在数据库事务中记录 `revision_id` 并原子更新远端 head；
  提交只更新控制元数据，不重复复制快照内容。
- `restore`：一次性 `workspace-restore-*` 任务把指定 revision 恢复到新的空卷；成功校验后才允许
  新 Worker 挂载，失败则删除未完成卷并保留原 head。
- `cleanup`：仅当没有 Worker、租约或进行中操作，最新本地代次已经 commit，且超过保留期时，
  才删除本地卷并进入 `REMOTE_ONLY`。

checkpoint 失败或结果不确定时，Workspace 返回 `DETACHED_DIRTY` 并保留本地唯一副本等待重试。
Manager 不保存进程、内存、临时运行状态和 Secret，只保存 `/workspace`。

## 受控文件操作

### 操作授权

Open WebUI/BFF 先校验当前用户、会话、对话和文件权限，再签发短期、单次使用的操作授权。
BFF 可以是 Open WebUI 的同源后端扩展，不要求新增独立公共服务。授权至少绑定：

```text
issuer + audience + operation + sandbox_id + workspace_id
+ file_id/source 或 workspace_path
+ destination/用途 + expires_at + nonce
```

Manager 校验签名、有效期、单次 nonce、Sandbox/Workspace 归属和当前租约，并持久记录消费结果。
因此猜到或篡改 `file_id` 不能扩大访问范围；Adapter 也不能只凭 `file_id` 请求文件。

### checkout：文件进入 Workspace

1. BFF 完成业务授权并签发 checkout 授权。
2. Manager 创建 `operation_id`，启动 `artifact-checkout-*` 一次性任务，只读访问获准源文件并
   读写挂载一个 Workspace。
3. 任务在挂载根内解析目标并拒绝符号链接穿越，通过受限 Files 接口读取对象，写入临时文件，
   校验大小和摘要后原子重命名。
4. Manager 记录完成结果；失败任务不得留下被误认为完整文件的目标路径。

### publish：生成物离开 Workspace

1. BFF 授权发布指定 `workspace_path`；Manager 对路径做规范化并绑定当前 Workspace。
2. `artifact-publish-*` 一次性任务只读挂载一个 Workspace，拒绝符号链接、目录和设备，并确认
   已打开的普通文件仍位于挂载根目录内。
3. 任务使用单次发布授权上传到 Open WebUI Files；Open WebUI 写入所有者、对话和文件元数据，
   返回 `file_id` 与稳定下载链接。
4. 链接每次下载仍由 Open WebUI 校验权限。已发布对象不随 Sandbox 或 Workspace 清理删除。

## 操作执行与恢复

一次性任务使用普通 `runc`，按操作命名，只获得一个 Workspace 挂载和一个受限网络目的地。
对象存储或 Open WebUI 的长期凭证，以及 Docker/Manager 凭证，均不得注入任务，更不得注入
Worker。

Manager 为每次操作持久化 `pending/running/succeeded/failed`、幂等键、输入绑定和结果。启动后
对账任务与记录：确定成功的操作复用结果，确定失败的安全重试，结果不明的操作先校验目标状态，
不得盲目覆盖或删除本地唯一副本。

## 明确不负责

- 用户、租户、对话和 `file_id` 的业务 ACL；
- Responses、Agent RPC/SSE、MCP Apps 数据面；
- 文件内容代理、通用对象存储 API 或浏览器下载服务；
- 向 Sandbox 提供对象存储、Open WebUI 或运行平台凭证。
