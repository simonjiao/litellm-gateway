# 文件与 Workspace 存储

本设计解决三类需求：对话中的用户文件与生成物、外部 MCP App 的受控 Artifact 交换，以及
Sandbox Workspace 的可恢复性。Artifact 是不可变对象，Workspace 使用 POSIX 文件系统。

## 存储分工

| 数据 | 权威存储 | 访问入口 |
|---|---|---|
| 用户、对话、笔记、文件元数据和业务 ACL | Open WebUI 数据库 | Open WebUI |
| Artifact 内容与不可变 manifest | 私有对象存储 | Artifact API；BFF/MCP capability |
| 消息绑定、publish intent 与业务引用 | Open WebUI 或调用方数据库 | 对应业务服务 |
| 活动 Workspace | 本地 POSIX 卷 | 对应 Worker；受控一次性任务 |
| 待发布候选副本 | Manager 本地持久卷 | 受控一次性任务；Worker 不挂载 |
| Workspace revision | 对象存储中的 restic 仓库 | Manager 编排的一次性任务 |
| Workspace/operation 状态 | Manager 持久数据库 | Manager 控制接口 |

浏览器、MCP App 和 Sandbox 不取得对象存储凭证。

生产部署中，Artifact 内容与 Workspace restic 仓库使用不同的 bucket/prefix 和服务凭证；
restic repository password 作为第三个独立 Secret 保存。Artifact Service 凭证只访问 Artifact
前缀；Manager 的 Workspace 父凭证只用于调用 STS。每个 Workspace 使用独立 restic
repository prefix，一次性快照任务只收到该 prefix 的临时会话凭证，不共享跨 Workspace 去重。

## Artifact Service

Artifact Service 是无状态文件服务，负责不可变 Artifact 的上传、查询、下载和删除；元数据
保存在 manifest 中，业务引用由调用方维护。最小接口为：

```text
create_upload → UploadTarget
complete_upload → ArtifactDescriptor
inspect(artifact_id) → ArtifactDescriptor
create_download(artifact_id) → DownloadTarget
delete(artifact_id) → Deleted  # 仅受信任业务服务
```

上传 PUT 流式写入临时对象，限制大小并增量计算 SHA-256；核验声明的大小和摘要后，提交
不可覆盖的内容对象，最后写不可变 manifest 作为提交标记。
`complete_upload` 只确认已有提交结果；`inspect` 和下载只接受已有 manifest 的对象。
上传失败时主动终止未完成分片；生命周期规则只回收上传临时前缀中的残留分片和临时对象，
不对正式内容与 manifest 设置过期删除。
manifest 只含 `artifact_id`、所有者、展示名、媒体类型、大小、摘要和创建时间；
`artifact_id` 是不透明标识，不包含对象键，也不是凭证。

`/healthz` 表示进程存活；`/readyz` 检查服务身份能否访问目标桶，成功返回 200，不可用返回
503，供部署判断存储依赖是否就绪。

Open WebUI/BFF 负责业务 ACL，并维护消息与 `artifact_id` 的绑定；Artifact Service 只验证可信
服务身份或短期 capability。Artifact 所有者在自身引用解除并经过宽限期后，才用受信任接口删除
对象；网关不推断跨系统引用，非所有者只能获得限时访问。外部 MCP App 的 capability 绑定
调用方、`app_id`、Artifact、操作、大小和有效期。控制和文件流均使用 HTTPS。
本地 `Path` 的 materialize/publish 不属于 Artifact API，而是 Manager 内部的 Workspace 文件
桥接能力。Artifact capability 是短期、单对象 Bearer，在有效期内允许传输重试，不是严格的
一次性 token；`storage-ops` 只短暂持有 `UploadTarget` 或 `DownloadTarget`，不持有 Artifact
Service 或对象存储长期凭证。Manager 自身带 nonce 的 operation grant 仍按单次消费处理。

外部 MCP App 通过 MCP Host 取得同一 HTTP 接口的短期目标；无需为文件复制再定义一套 MCP
工具协议，MCP 消息也不承载大文件字节。Host 注入调用方和 `app_id`，App 不能自行声明身份、
对话绑定或扩大 Artifact 范围；消息附件绑定仍只能由 BFF 完成。

受信任外部服务通过版本化 HTTP 契约接入，以独立 Artifact Service 实例、凭据和对象前缀
隔离业务；其可信 BFF/Gateway 负责用户认证与业务 ACL，调用方只能在授予的 Artifact 范围内
操作。服务凭据和内部传输目标不进入模型输入或用户下载链接。

## 对话绑定

同源 BFF 在 Open WebUI 数据库中维护对话与 Workspace 映射、publish intent 和消息 Artifact
绑定；用户不能直接修改这些映射。记录保存业务控制信息与不透明 ID，访问权依据 Open WebUI
ACL 判断。Workspace 的创建、复用和解除映射见 [设计总览](design.md#对话与-workspace-绑定)。

## Workspace 目录约定

Open WebUI 在调用 Responses 前建立并持久化用户消息和助手占位消息；文件目录直接使用经 BFF
校验的消息 ID：

```text
/workspace/uploads/<user_message_id>/       该用户消息的附件，Agent 只读
/workspace/work/                            持久工作目录，Agent 默认在此运行
/workspace/outputs/<assistant_message_id>/  该助手消息可发布的生成物
/tmp/                                       不持久化的临时文件
```

BFF 验证用户消息和助手消息属于同一对话链，并在助手消息的服务端 metadata 中保存
`response_id`。可信控制面只把完整输入、输出路径注入当前 Agent 执行。挂载或文件权限强制输入
只读；当前消息的发布范围由授权和安全路径解析强制。只有终态回复明确引用并成功 publish 的
输出成为 Artifact。checkpoint 保存整个 `/workspace`，不保存 `/tmp`；Manager 的候选暂存区不挂载
给 Worker，也不进入 checkpoint。

## 对话附件上传与下载

```text
上传：Browser → 同源 BFF → Artifact Service → 对象存储
下载：Browser ← 同源 BFF ← Artifact Service ← 对象存储
```

BFF 和 Artifact Service 以有界内存缓冲和背压逐块转发文件，上传解析及下载均不落本地磁盘。
Open WebUI 只保存文件元数据和 `file_id → artifact_id` 映射，文件内容统一由 Artifact 保存。

Artifact 提交且文件记录与映射持久化后，才返回上传成功；用户发送消息时再建立消息附件绑定。
用户上传和 Agent 生成物统一使用该文件记录，附件卡片及正文中的下载链接均指向同源 BFF；
已发布的 `sandbox:` 候选替换为稳定应用链接。
每次下载重新校验文件所有者或对话访问权限，短期传输票据仅由 BFF 内部使用。

Open WebUI 不承担知识库/RAG 和附件自动解析，文件解析由 Agent 在 Workspace 中完成；
Workspace 的文件落盘遵循 checkout/publish 约定。

## 文件进入 Sandbox

Sandbox 不能凭 `artifact_id` 直接读取文件。BFF 校验当前用户、对话和 Artifact 绑定后，签发
绑定 `sandbox_id`、`workspace_id`、`user_message_id`、`assistant_message_id`、源 Artifact 集合、
大小/摘要、有效期和 nonce 的单次 checkout 授权。消息被接受后，Adapter 可以先创建 Sandbox，
但必须等待 checkout 成功才提交 Agent 执行。

Manager 启动一个 `artifact-checkout-*` 批次任务，将全部附件暂存到同一 Workspace 文件系统，
逐个校验后原子提交为 `uploads/<user_message_id>`，同时准备当前助手消息的空输出目录。任一附件
失败时整批不可见且 Agent 不执行；重试通过 `operation_id`、幂等键和 manifest 对账，具体约束
见 [Sandbox Manager 设计](sandbox-manager.md)。

## 生成物发布与下载

Agent 将生成物写入当前 `outputs/<assistant_message_id>`，并在回复中使用
`sandbox:/workspace/outputs/<assistant_message_id>/<relative_path>` 表示候选文件。该 URI 不是下载
凭证。BFF 在终态 Response 中只识别这些明确候选，校验用户、对话、
`assistant_message_id → response_id` 和相对路径后，按候选事务性创建唯一发布记录
（publish intent）并立即驱动 Manager。

Manager 安全打开当前消息目录中的普通文件，复制到 Worker 不可见的本地暂存区，校验文件在
复制期间未变化，并以摘要和原子 manifest 提交稳定副本。该暂存区持久化且不进入 Workspace。
BFF 阻塞该 Workspace 的下一 Turn 到 Manager 报告捕获完成或失败。捕获失败时该候选失败，
捕获成功后上传可在后台重试。每次上传尝试单独取得新的短期
`UploadTarget`；本地稳定副本支持 Artifact Service 故障后的重试。

publish intent 持久化 `pending/captured/uploading/uploaded/ready`、`operation_id`、尝试次数和下次
重试时间；临时上传失败进入 `retryable`，Artifact 已提交但消息绑定失败进入 `binding_retry`，
路径非法、文件缺失、变化或超限进入 `failed`。相同幂等键始终复用原操作和 Artifact。

事件触发是主路径；用户点击未就绪候选时立即推进同一 intent，BFF 周期任务查询到期记录进行
补偿。重试根据持久化 intent 和当前消息绑定签发新 nonce 的短期授权，并保持原幂等键。只有
`ready` 返回鉴权下载，其他状态不暴露对象地址。稳定副本保留到 Artifact manifest 提交。
已绑定 Artifact 不随 Sandbox 或 Workspace 清理删除。

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

存储实现复用兼容 S3 的私有对象存储、restic 和标准 HTTP 流式传输。checkout/publish 由 Manager
编排同一个 `storage-ops` 一次性任务镜像；它不是常驻服务。外部 MCP App 直接使用受限 Artifact
接口，不选择 `workspace_path`；只有文件进出 Sandbox 时才经过 Manager Bridge。
