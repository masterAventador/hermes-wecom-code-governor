# Hermes 企微 Codex 代码治理器

这是“魏帅·代码机器人”的 Hermes 治理插件。Hermes 负责企业微信消息、会话、项目选择和
受控项目读取；只有修改代码时，插件才调用本机 Codex App Server 原生运行时执行。

## 当前架构

```text
企业微信
  → Hermes：消息、引用、附件、普通对话、项目卡片
  → 本插件：userid + chatid 权限、项目范围、受控只读/任务工具、worktree 生命周期
  → Codex App Server：仅负责受控 worktree 内的文件修改和测试
  → 本插件：变更数量检查、项目验证、提交、快进合并、清理
```

Hermes 外层使用默认 agent loop 完成对话和项目分析，但不能调用原生文件或终端工具绕过治理。
项目读取走 `governor_project_files`、`governor_project_read`、`governor_project_search` 和
`governor_project_git`；测试、打包、导出等非源码修改动作走 `governor_project_job`；只有代码
修改走 `governor_codex_change`，启动或恢复本地 Codex 原生线程。

## 安全边界

- 未通过 `userid + chatid` 白名单的企微消息在进入模型前静默丢弃。
- 每个权限组只能访问配置中授权的项目或根目录。
- 修改前由脚本创建独立 Git worktree，Codex 不直接修改基准工作区。
- Codex 使用命名 permission profile：只读最小系统路径和授权根目录，只写当前 worktree
  及该 worktree 对应的 Git 管理目录。
- Codex 命令网络默认关闭，所有提权审批自动拒绝；企微和对象存储密钥不会传给 Codex 子进程。
- `.env` 与 `.env.*` 在 Codex 权限组中显式禁止读取。
- Codex 不负责 `commit`、`merge`、`push` 或 worktree 管理。
- 只有 Codex 返回结构化的 `completed`，脚本验证通过后才会提交并快进合并；需要补充信息时
  返回 `needs_input`，保留 worktree 和同一个 Codex thread 等待下一条消息。
- 打包、测试和导出只有用户明确要求时才能执行，并且命令必须由管理员预先登记；任务在一次性
  隔离 worktree 中运行，不能修改原仓库、读取个人文件、继承机器人密钥或访问公网。
- 部署和推送没有隐式入口，未配置受控命令时不能执行。
- 既有文件或任务产物不超过 50MiB 时由企微原生发送，超过后上传腾讯云并回复七天临时链接。

## 环境要求

- macOS
- Python 3.11～3.13
- Hermes Agent `0.20.3`（官方稳定版 `v2026.8.16.2`）
- `project_discovery.enabled: true` 时，会在权限组 `roots` 下自动发现 Git 仓库；显式
  `projects` 配置仍用于覆盖友好名称、基准分支、校验与打包设置。
  每次使用项目搜索、选择、读取或执行工具前都会重新扫描，新增或删除 Git 仓库不需要维护
  项目清单，也不需要重启网关；只有修改显式项目的专用配置时才需要重启。
- 受信任用户只需加入对应权限组的 `users` 列表。治理版企微适配器直接使用这份权限，
  无需在代码中写死 userid；修改配置后重启网关生效。
- 本机可用的 Codex CLI
- 已执行 `codex login`，本机 `~/.codex/auth.json` 可用；不需要 OpenAI API Key

当前本机分层执行参数：

- Hermes 外层普通对话、项目读取与任务编排使用 `gpt-5.6-sol + medium`，配置在
  [config/hermes.config.local.yaml](config/hermes.config.local.yaml)。
- 只有修改代码时由 `governor_codex_change` 启动的第二层 Codex 执行器使用
  `gpt-5.6-sol + xhigh`，配置在 [config/governor.local.yaml](config/governor.local.yaml)。
- 第二层 Codex CLI：`/opt/homebrew/bin/codex`。

## 安装

本机 Hermes 安装在 `/Users/aventador/.hermes/hermes-agent`，治理插件以 editable 方式安装到
Hermes 自己的 Python 环境。重新安装插件时执行：

```bash
/Users/aventador/.hermes/bin/uv pip install \
  --python /Users/aventador/.hermes/hermes-agent/venv/bin/python \
  -e /Users/aventador/sourceCode/hermes-wecom-code-governor
```

实际 Hermes 配置是 [config/hermes.config.local.yaml](config/hermes.config.local.yaml)，运行目录是
`.runtime/hermes-home`。`config.yaml` 以符号链接指向前述配置；Bot、COS 和 Hermes OAuth 凭据
都在运行目录中，不写入仓库。

首次安装或重装 macOS 常驻服务：

```bash
./scripts/install-service.sh
```

日常启停和状态检查：

```bash
./scripts/start.sh
./scripts/stop.sh
./scripts/status.sh
```

服务由 macOS `launchd` 托管，登录后自动启动，异常退出后自动拉起。旧 Node 服务的 `.env`
已经迁移到 `.runtime/hermes-home/.env`，旧项目不会再持有 Bot 密钥。

Hermes 配置中的 `model.openai_runtime` 保持 `auto`。这是有意设计：Hermes 默认循环只负责
外围编排，项目任务通过插件内部的 Codex SDK 连接 App Server，而不是让 Hermes 全局切换到
Codex runtime。

## 测试

常规回归：

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check hermes_wecom_code_governor tests
.venv/bin/ruff format --check hermes_wecom_code_governor tests
```

挂载真实 Hermes 运行时执行企微适配器集成测试：

```bash
PYTHONPATH=/Users/aventador/.hermes/hermes-agent \
  .venv/bin/python -m pytest -q tests/test_wecom_adapter_integration.py
```

使用本机 ChatGPT 订阅进行真实 Codex 验收：

```bash
RUN_CODEX_LIVE=1 .venv/bin/python -m pytest -q tests/test_codex_live.py -s
```

该验收会在系统临时目录创建 Git 仓库和 worktree，真实启动 Codex App Server，连续执行两轮
同线程任务，随后验证合并并清理；不会修改任何已登记业务项目。

腾讯云真实上传与签名下载验收：

```bash
RUN_COS_LIVE=1 \
GOVERNOR_SECRET_ENV_FILE=.runtime/hermes-home/.env \
.venv/bin/python -m pytest -q tests/test_cos_live.py
```

当前腾讯云子账号具备上传和下载权限，但没有 `DeleteObject` 权限；验收对象会复用固定键，
不会每次生成新的残留对象。

## 关键文件

- [hermes_wecom_code_governor/runtime.py](hermes_wecom_code_governor/runtime.py)：企微会话治理与工具路由
- [hermes_wecom_code_governor/codex_runtime.py](hermes_wecom_code_governor/codex_runtime.py)：Codex App Server 客户端与 permission profile
- [hermes_wecom_code_governor/worktree.py](hermes_wecom_code_governor/worktree.py)：worktree、验证、合并和清理
- [hermes_wecom_code_governor/project_job.py](hermes_wecom_code_governor/project_job.py)：非修改任务的隔离执行和产物暂存
- [hermes_wecom_code_governor/state.py](hermes_wecom_code_governor/state.py)：项目及 Codex thread 持久化
- [hermes_wecom_code_governor/delivery.py](hermes_wecom_code_governor/delivery.py)：企微 50MiB 与腾讯云交付
- [scripts/status.sh](scripts/status.sh)：本机常驻网关状态检查
- [docs/design.md](docs/design.md)：完整设计说明
