# 文件与 Workspace 存储

本设计只解决两类数据：对话中的用户文件与生成物，以及 Sandbox Workspace 的可恢复性。
不新增通用存储产品或文件系统服务。

## 存储分工

| 数据 | 权威存储 | 访问入口 |
|---|---|---|
| 用户、对话、笔记、ACL、文件元数据 | Open WebUI 数据库 | Open WebUI |
| 用户上传与已发布生成物 | Open WebUI Files + 私有对象存储 | Open WebUI/BFF 鉴权链接 |
| 活动 Workspace | 本地 POSIX 卷 | 对应 Worker；受控一次性任务 |
| Workspace revision | 对象存储中的 restic 仓库 | Manager 编排的一次性任务 |
| Workspace/operation 状态 | Manager 持久数据库 | Manager 控制接口 |

Open WebUI 配置 S3 只改变其文件对象后端，不会把笔记或对话正文迁移到 S3。对象存储无需让
浏览器或 Sandbox 直接访问；它可以仅在内网提供服务。其他系统可用独立 bucket/prefix 和凭证
复用该对象存储，但各自维护业务 ACL。

生产部署中，Open WebUI 文件对象与 Workspace restic 仓库使用不同的 bucket/prefix 和服务
凭证；restic repository password 作为第三个独立 Secret 保存。Open WebUI 独占 Files 凭证；
Manager 的专用 Workspace 父凭证只用于调用 RustFS STS。每个 Workspace 使用独立 restic
repository prefix，一次性任务只收到该 prefix 的临时会话凭证，不共享跨 Workspace 去重。

本地验收可由 `configure-rustfs.py` 导入现有 rclone 业务 AK/SK，并显式使用 `static` 模式；
凭证只进入 Manager 创建的受信任一次性任务，任务结束即删除，Worker 不可见。生产环境使用
可调用 `AssumeRole` 的独立 IAM 凭证和默认 `sts` 模式。

文件后端只向上层提供上传会话、上传完成、元数据查询和短期下载源；本地 `Path` 的 materialize
与 publish 不属于该接口，而是 Manager 内部的 Workspace 文件桥接能力。当前文件后端是 Open
WebUI Files，`storage-ops` 只消费单次传输 URL 和令牌；该边界允许以后替换后端，但本版不部署
独立 Artifact Service。

## 对话绑定

同源 BFF 在 Open WebUI 数据库中维护不可由用户修改的 `chat_workspaces` 映射表。记录只保存
`chat_id`、不透明 `workspace_id`、策略和时间戳；访问权每次从 Open WebUI 对话 ACL 重新判断。

- 对话首次执行 Agent 时，BFF 经 Adapter 请求 Manager 创建可恢复 Workspace，再保存映射；
- 后续请求取得同一 `workspace_id` 并注入短期签名授权，Adapter 转交给 Manager；
- 无已认证对话上下文时不创建映射，Manager 使用实例级临时 Workspace；
- 克隆或分叉对话默认创建新的空 Workspace，不继承原 Workspace；
- 删除对话时解除映射并请求停止活动租约，Workspace 按保留策略延迟删除。

BFF 以 Open WebUI v0.11.1 派生镜像中的薄路由实现，直接复用其用户认证、Chats、Files 和
Storage，不部署独立公共服务，也不复制 Open WebUI 的文件元数据或 ACL。

## Workspace 目录约定

可信请求路径为每轮分配 `turn_id`，浏览器和 Agent 均不能修改：

```text
/workspace/uploads/<turn_id>/   当前消息附件，Agent 只读
/workspace/work/                持久工作目录，Agent 默认在此运行
/workspace/outputs/<turn_id>/   当前 Turn 可发布生成物
/tmp/                           不持久化的临时文件
```

挂载或文件权限强制目录归属；操作授权和安全路径解析决定文件能否进入或离开 Workspace。目录
名称不替代授权，写入 `outputs` 也不会自动发布文件。checkpoint 保存整个 `/workspace`，不保存
`/tmp`；同卷的 Manager 私有 staging 在 checkpoint 前清理或排除。

## 用户上传与下载

```text
上传：Browser → Open WebUI auth/ACL → Files API ─┬→ Open WebUI DB（元数据）
                                                  └→ object storage（内容）

下载：stable link → Open WebUI auth/ACL → Files API → object storage → Browser
```

Open WebUI 把上传结果作为文件附件绑定到对话消息，并返回稳定 `file_id` 或应用链接；每次下载
都重新校验用户与对话权限。默认不向浏览器返回对象存储长期凭证或永久公开 URL；需要大文件
卸载时，只能在鉴权后签发短期、单对象 URL。

## 文件进入 Sandbox

Sandbox 不能凭 `file_id` 直接读取文件。BFF 校验当前用户、对话和文件 ACL 后，签发绑定
`sandbox_id`、`workspace_id`、`turn_id`、源文件集合、大小/摘要、有效期和 nonce 的单次 checkout
授权。消息被接受后，Adapter 可以先创建 Sandbox，但必须等待 checkout 成功才提交本轮 Agent
任务。

Manager 启动一个 `artifact-checkout-*` 批次任务，将全部附件暂存到同一 Workspace 文件系统，
逐个校验后原子提交为 `uploads/<turn_id>`。任一附件失败时整批不可见且本轮不执行；重试通过
`operation_id`、幂等键和 manifest 对账，具体约束见 [Sandbox Manager 设计](sandbox-manager.md)。

## 生成物发布与下载

Agent 将生成物写入当前 `outputs/<turn_id>` 并关闭文件；用户或受信任上层随后调用
`POST /api/agent/artifacts/publish`。系统不扫描目录，也不自动发布。BFF 只授权与目标助手消息
绑定的 Turn 和精确路径，Manager 短暂停止写入并固化只读快照，随后由 `artifact-publish-*`
从快照上传到 Open WebUI Files。

完整上传成功后，BFF 才把返回的 `file_id` 和稳定下载链接附加到对应助手消息。失败或结果不明
时不暴露不完整文件；未绑定对象延迟回收，幂等重试不能产生重复附件。发布对象具有独立生命
周期，即使 Sandbox 销毁或 Workspace 本地卷被清理，链接仍可按 ACL 下载。

## Workspace 持久化

活动 Workspace 始终使用本地 POSIX 卷，Agent 读写不经过对象存储。Sandbox 销毁后，可恢复
Workspace 先停止写入，再由后台 `workspace-checkpoint-*` 任务用 restic 增量保存到对象存储；
成功提交 revision 后，Manager 按保留期延迟删除本地卷。

restic 负责文件扫描、增量分块和去重，系统不维护额外的变更日志或同步索引。

恢复时，`workspace-restore-*` 把选定 revision 写入新的空卷并校验，成功后才启动 Worker。
checkpoint 失败或结果不明时保留 dirty 本地卷，绝不删除唯一副本。快照仅包含 `/workspace`，
不包含进程、内存、临时运行状态或 Secret。
对话解除引用后，Manager 先标记延迟删除；宽限期到期且无活动租约或操作时，再删除该
Workspace 的本地卷、repository prefix 和控制记录。

存储实现复用 Open WebUI Files、兼容 S3 的私有对象存储和 restic。checkout/publish 由 Manager
编排同一个 `storage-ops` 一次性任务镜像；它不是常驻服务，也不构建通用文件 API。外部 MCP
App 的 AppSession 或 `file_id` 不授予 Workspace、Files 或对象存储访问权；独立通用 Artifact
Service 不属于本设计。
