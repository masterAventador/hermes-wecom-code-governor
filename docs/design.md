# Hermes 企微 Codex 代码治理插件设计

## 目标

在不修改 Hermes 官方源码的前提下，为企微 AI Bot 增加本机代码操作治理，并让项目任务
在代码修改时使用 Codex 原生 App Server harness：

- 未授权用户的消息在进入模型前静默丢弃。
- 权限由 `userid + chatid + project/root` 共同决定。
- Hermes 外层 AI 判断请求是否需要访问项目；普通聊天和自我介绍直接回答。
- 需要项目但项目不明确时，使用企微项目选择卡片。
- 项目答疑由 Hermes 外层使用插件提供的结构化只读工具完成；只有代码修改委托本机 Codex
  App Server。
- 代码修改只允许发生在独立 Git worktree；验证通过后快进合并回基准分支并清理。
- 默认只分析或修改代码。测试、打包、导出等本地动作必须由用户在当前请求中明确要求，并通过
  通用受控任务通道执行管理员预先登记的命令；部署和推送没有默认入口。
- 文件上传既可以交付当前已选项目内的既有文件，也可以交付受控任务生成并暂存的产物。
- 权限、文件根目录、网络、审批和 Git 生命周期由代码护栏校验，提示词只负责协作体验。
- 提示词不保存项目清单或数量；需要项目时由治理工具实时扫描授权根目录，新增和删除仓库会在
  下一次项目工具调用时生效。

## 信任边界

Hermes 官方能力负责企微长连接、消息引用、附件、普通会话和项目选择交互。本插件负责业务
授权、项目状态、结构化只读工具、通用项目任务、修改任务 Codex thread、worktree 生命周期、
App Server permission profile 和企微项目卡片。Codex App Server 只负责代码修改和测试工具循环。

模型输出、用户消息、仓库文件和附件均视为不可信输入。Hermes 外层只能通过插件的只读工具
读取当前已选项目，不能使用原生终端或文件工具绕过边界；Codex 只能使用修改任务生成的
permission profile。模型不能自行扩大项目范围，也不能把任意命令声明成“打包”或“部署”来
绕过配置。任务命令和允许交付的产物规则只能来自管理员维护的项目配置。

远程受控动作（`governor_remote_task`）是唯一跨出本机沙箱、直接出网并使用本机 SSH 私钥的
路径，因此单独收敛：目标主机与命令 argv 完全来自项目配置的 `remote_actions`，模型只能按
登记名称触发、无法拼接主机或命令。执行强制 `BatchMode=yes` + `StrictHostKeyChecking=yes`，
清空子进程环境仅保留 PATH（机器人密钥不外传），并受命令级超时约束。触发受同一套授权约束
（授权身份 + 已选项目 + 无活动改码任务），每次执行前后写入含触发者身份的审计日志。

## 文件交付

模型只有在用户明确要求发送当前项目中的既有文件时，才能调用 `governor_deliver_file`。
脚本会解析真实路径并拒绝路径穿越，文件必须位于当前已选项目目录中。通用任务生成的产物
由 `governor_project_job` 自动暂存和交付，模型不能指定管理员未登记的产物路径规则。

- 文件不超过 `52,428,800` 字节：使用企微 AI Bot 原生分片上传并发送文件消息。
- 文件超过该阈值：上传腾讯云存储桶，最终回复中附带七天有效的签名下载地址。
- 待交付文件绑定发起消息的 message id，群聊并发时不会串给另一个人的回复。
- 打包和部署不会因文件交付而自动发生；只有用户明确要求且项目预先登记了对应命令才会执行。

## 受控执行状态

每个 Hermes `session_key` 最多绑定一个当前项目和一个活动任务：

1. 没有项目：允许普通问答；Hermes 项目文件和终端工具被阻止。
2. 已选项目：外层 Hermes 使用结构化只读工具直接检查项目，不启动第二层 Agent。
3. 需要执行非修改任务：`governor_project_job` 创建一次性 detached worktree，在 Codex 沙箱中
   运行精确匹配的管理员命令，完成后暂存允许的产物并删除 worktree；不启动内层 Agent。
4. 需要触发远程受控动作：`governor_remote_task` 按登记名称 ssh 到固定主机执行固定命令，
   取回截断后的输出；不创建 worktree、不启动内层 Agent，活动改码任务存在时拒绝执行。
5. 需要修改：`governor_codex_change` 创建 worktree，以写权限组启动或恢复任务线程。
6. Codex 返回 `needs_input`：保留 worktree 和 thread，等待用户后续消息。
7. Codex 返回 `completed`：脚本检查变更范围、运行验证、提交并快进合并到基准分支。
8. 验证成功：清理 worktree 和任务分支。
9. 验证或合并失败：保留 worktree 和 thread，禁止宣称已经合并。

## Codex 原生执行路线

Hermes 外层使用 `openai-codex` 订阅 OAuth 与默认工具循环，负责对话、只读项目分析和调用治理
工具。只有 `governor_codex_change` 通过官方 `openai-codex` Python SDK 的 JSON-RPC 客户端启动
本机 Codex App Server；模型为 `gpt-5.6-sol`，推理强度为 `xhigh`。

不把 Hermes 全局切换为 `codex_app_server`，因为全局模式下 Codex 内建 `shell/apply_patch` 不经过
Hermes 插件的 `pre_tool_call`。本设计把 Codex 作为受控的工程执行器：管控层先确定项目、创建
worktree 并生成最小权限组，然后才启动 Codex turn。

## Permission profile

每次 App Server 进程只收到当前任务动态生成的 `hermes-governor` 权限组：

- `:minimal` 仅只读，满足系统命令运行需要。
- 修改任务只写当前 worktree 和它专属的 Git worktree 管理目录；公共 `.git` 只读。
- `.env` 与 `.env.*` 显式拒绝读取。
- 命令网络关闭。
- `approvalPolicy=never`，SDK 的审批回调也固定返回拒绝。
- 子进程通过 `env -i` 启动，只传运行必需环境；企微和 COS 凭据不进入 Codex 环境。

## 会话恢复

Hermes `session_key` 对应一个持久化会话记录：

- 活动修改任务保存独立 Codex thread id，并绑定唯一 worktree。
- 项目追问继续使用 Hermes 外层会话；任务需要补充信息时恢复活动任务线程。
- 合并成功后清除任务线程。
