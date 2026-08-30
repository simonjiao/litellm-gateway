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
| `turn_id` | 可信请求路径分配的一轮执行身份，用于隔离输入和可发布输出 |
| `revision_id` | 已提交到远端仓库的不可变 Workspace 快照 |
| `operation_id` | checkout、publish、checkpoint、restore 或 retire 的幂等操作 |

一个可恢复 Workspace 同时最多有一个可写 Worker。Manager 使用 Workspace 锁和本地代次阻止
旧 Worker 或重复任务继续写入；同一 Workspace 的 Turn 串行执行，文件操作必须绑定活动
Sandbox 和对应 Turn。

## 控制接口

| 接口组 | 语义 |
|---|---|
| Workspace | 创建、查询、checkpoint、restore 和解除引用；用户触发的请求要求 BFF 授权 |
| Sandbox | 创建、查询、续租和销毁；创建时挂载一个可恢复 Workspace 或新建临时 Workspace |
| 文件操作 | checkout 和 publish；返回可查询的 `operation_id`，不承载文件内容 |
| 操作查询 | 返回状态和结果；相同幂等键复用已有操作 |

浏览器不直接调用这些接口。Open WebUI/BFF 完成业务授权后调用 Adapter，Adapter 只转交签名
授权和控制请求；Manager 独立验证授权。

## Sandbox 生命周期

Manager 提供创建、查询、续租和销毁能力：

1. 校验调用方和请求；若给出有效的 Workspace 授权则验证并挂载，未给出则创建实例级临时
   Workspace；生成不可预测的 `sandbox_id` 与独立连接凭证。
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
- `retire`：Workspace 解除所有对话引用后记录 `delete_after`；宽限期内仍可恢复，到期且
  无活动资源后，由一次性任务删除该 repository prefix，再清理本地卷和控制记录。

checkpoint 失败或结果不确定时，Workspace 返回 `DETACHED_DIRTY` 并保留本地唯一副本等待重试。
Manager 不保存进程、内存、临时运行状态和 Secret，只保存 `/workspace`。

## 持久化实现

Manager 采用单实例部署，使用标准库 SQLite、WAL 模式和独立持久卷，不增加 ORM 或外部数据库
服务。数据库保存 `workspaces`、`revisions`、`sandboxes`、`operations` 和 `consumed_nonces`；
文件内容与 Open WebUI 的 `chat_id → workspace_id` 映射不进入 Manager 数据库。

状态转换、head revision 更新、Workspace 单写者锁和 nonce 消费必须在事务中完成。Manager
启动时以数据库记录和受管 Docker 标签进行双向对账：保留未确认已备份的卷，回收无有效记录的
Worker 和一次性任务，并恢复可安全重试的操作。
自动 detach、checkpoint、cleanup 和 retire 是 Manager 状态机的内部维护操作，不依赖
在线用户令牌。

## Workspace 目录与 Turn 边界

可信请求路径为每轮分配不可由浏览器或 Agent 指定的 `turn_id`，Manager 准备固定目录：

| 路径 | 写入者 | 用途 |
|---|---|---|
| `/workspace/uploads/<turn_id>` | 一次性任务 | 当前消息附件；Agent 只读 |
| `/workspace/work` | Agent | 项目文件和持久中间结果；默认工作目录 |
| `/workspace/outputs/<turn_id>` | Agent | 当前 Turn 可发布生成物；Turn 结束后封存 |
| `/tmp` | Agent | 不进入 checkpoint 的临时文件 |

目录归属和只读属性由挂载或文件权限强制，提示词只说明用法。Workspace 顶层由 Manager 管理；
checkout 不能选择其他目的目录，publish 不能读取 `work`、`uploads` 或其他 Turn 的输出。所有
外部路径都以受控目录文件描述符为根解析，拒绝绝对路径、`..`、符号链接和非普通文件。Manager
准备目录，Adapter 向 Agent 注入本轮输入和输出路径；同卷的私有 staging 不向 Agent 开放，并在
checkpoint 前对账清理。

## 受控文件操作

### 操作授权

Open WebUI/BFF 先校验当前用户、会话、对话和文件权限，再签发短期、单次使用的操作授权。
BFF 可以是 Open WebUI 的同源后端扩展，不要求新增独立公共服务。授权至少绑定：

```text
issuer + audience + operation + sandbox_id + workspace_id
+ turn_id + file_id/source 或 workspace_path
+ destination/用途 + max_bytes/摘要 + expires_at + nonce
```

Manager 校验签名、有效期、单次 nonce、Sandbox/Workspace 归属和当前租约，并持久记录消费结果。
因此猜到或篡改 `file_id` 不能扩大访问范围；Adapter 也不能只凭 `file_id` 请求文件。

授权使用独立密钥的 HMAC-SHA256 紧凑令牌；Adapter 只能转交令牌，不持有签名密钥。Manager 在
创建操作时原子消费 nonce，并为一次性任务生成仅限该 `operation_id` 的短期传输凭证。

checkout/publish 任务使用 BFF 验证的单次 Files 传输令牌。checkpoint/restore 任务使用
Manager 通过 RustFS STS 签发的临时 S3 会话凭证；会话策略同时限制当前 Workspace
repository prefix、必需动作和任务时间。父凭证只注入 Manager，不注入一次性任务或
Worker；不使用 RustFS root 凭证。

### checkout：文件进入 Workspace

当前消息被接受后，Adapter 先创建或恢复 Sandbox，但在 checkout 成功前不提交本轮 Agent 任务：

1. BFF 对当前消息的全部附件签发一个批次授权，目标固定为 `uploads/<turn_id>`。
2. Manager 创建 `operation_id`；`artifact-checkout-*` 任务把全部文件下载到同一 Workspace 内的
   Manager 私有 staging 目录，逐个校验大小、摘要和类型。
3. 全部成功并持久化后，在同一文件系统内将 staging 目录原子重命名为最终目录，再记录成功；
   Agent 因而只会看到全部附件或完全看不到本批附件。
4. 任一文件失败则不启动本轮 Agent，并清理 staging。提交后崩溃时，Manager 依据 manifest 和
   幂等键对账完成状态，不重复覆盖已提交目录。

### publish：生成物离开 Workspace

publish 不监听目录，只能由已认证的上层请求显式触发，且生产者必须已经关闭文件：

1. BFF 授权与目标助手消息绑定的 `outputs/<turn_id>` 内精确相对路径；Manager 校验 Turn、
   Sandbox 和 Workspace 仍匹配，并取得排他的文件操作锁。
2. Manager 短暂停止该 Workspace 的写入；一次性任务安全打开普通文件，复制为 Manager 管理的
   只读快照并计算摘要，然后恢复写入。网络上传只读取快照。
3. `artifact-publish-*` 使用单次授权上传到 Open WebUI Files。完整上传成功后，Open WebUI 才
   写入所有者和文件元数据、附加助手消息，并返回 `file_id` 与稳定下载链接。
4. 上传或消息绑定失败时不暴露不完整链接；未绑定对象延迟回收，相同幂等键复用已完成结果。
   已成功发布的对象不随 Sandbox 或 Workspace 清理删除。

## 操作执行、恢复与监控

一次性任务使用普通 `runc`，按操作命名，最多获得一个 Workspace 挂载和一个受限网络目的地。
对象存储或 Open WebUI 的长期凭证，以及 Docker/Manager 凭证，均不得注入任务，更不得注入
Worker。临时凭证仅在任务存活期内可用，任务终止后不持久化。

Manager 为每次操作持久化 `pending/running/succeeded/failed`、幂等键、输入绑定和结果。启动后
对账任务与记录：确定成功的操作复用结果，确定失败的安全重试，结果不明的操作先校验目标状态，
不得盲目覆盖或删除本地唯一副本。公开状态保持简单，内部 manifest 区分 staging、文件提交、
对象上传和消息绑定等恢复点。

创建操作返回 `operation_id`，Adapter 查询到终态；checkout 失败阻止当前 Turn，publish 失败
不附加文件。Manager 的结构化日志、任务标签和指标至少关联 `operation_id`、操作类型、状态、
耗时、字节数和错误码，不记录传输令牌、URL 或存储凭证。

## 明确不负责

- 用户、租户、对话和 `file_id` 的业务 ACL；
- Responses、Agent RPC/SSE、MCP Apps 数据面；
- 文件内容代理、通用对象存储 API 或浏览器下载服务；
- Workspace 目录监听、自动发布或独立通用 Artifact Service；
- 向 Sandbox 提供对象存储、Open WebUI 或运行平台凭证。
