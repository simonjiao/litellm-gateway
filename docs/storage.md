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
`sandbox_id`、`workspace_id`、源文件、目标路径、有效期和 nonce 的单次 checkout 授权。
Manager 校验绑定并启动 `artifact-checkout-*` 任务；任务只挂载一个 Workspace，通过受限 Files
接口读取文件，临时写入、校验后原子重命名。具体约束见 [Sandbox Manager 设计](sandbox-manager.md)。

## 生成物发布与下载

Agent 先把生成物写入 `/workspace`。用户选择发布后，BFF 授权具体路径；Manager 启动只读的
`artifact-publish-*` 任务，确认规范化后的普通文件仍位于 Workspace 内，再上传到 Open WebUI
Files。Open WebUI 返回 `file_id` 和稳定下载链接，BFF 将其作为文件附件写入对应对话消息。
发布后的对象具有独立生命周期，即使 Sandbox 销毁或 Workspace 本地卷被清理，链接仍可按
ACL 下载。

## Workspace 持久化

活动 Workspace 始终使用本地 POSIX 卷，Agent 读写不经过对象存储。Sandbox 销毁后，可恢复
Workspace 先停止写入，再由后台 `workspace-checkpoint-*` 任务用 restic 增量保存到对象存储；
成功提交 revision 后，Manager 按保留期延迟删除本地卷。

restic 负责文件扫描、增量分块和去重，系统不维护额外的变更日志或同步索引。

恢复时，`workspace-restore-*` 把选定 revision 写入新的空卷并校验，成功后才启动 Worker。
checkpoint 失败或结果不明时保留 dirty 本地卷，绝不删除唯一副本。快照仅包含 `/workspace`，
不包含进程、内存、临时运行状态或 Secret。

存储实现复用 Open WebUI Files、兼容 S3 的私有对象存储和 restic。checkout/publish 使用受限
HTTP 客户端和一次性任务，不设置常驻传输服务，也不构建通用文件 API。
