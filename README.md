# AgentNews

AgentNews 是一个个人向的 AI 新闻日报 Agent。仓库里包含 Python 项目骨架、
SQLite 迁移脚本、受管本地文件工具、Tavily MCP 搜索适配、Markdown 日报存储
以及一个最小的命令行入口。

Agent 编排基于 LangChain 的工具调用：运行时会自动发现已配置 MCP 服务对外
发布的工具，并把这些工具与四个受管本地文件工具一起注册给模型。项目还自带
一个 `bash(command, explanation)` 工具——每次调用都会先暂停，等待本机用户
人工审核，通过后才用当前 Windows 用户权限启动本地解析出来的 PowerShell 进程。
解释器优先使用 PowerShell 7（`pwsh`），未安装时回退到系统自带的
Windows PowerShell 5.1。

FastAPI 后端提供对话、设置、一次性日报、日报、来源和健康检查接口，默认使用
SQLite 做业务持久化；测试和本地集成场景仍可显式传入 `InMemoryRepository`。

单页前端（`/`）用于填写身份与订阅偏好、设置每日发送时间，并浏览、阅读、
删除和邮件发送已生成的日报。

## 环境要求

- Python 3.14 或更高版本
- [uv](https://docs.astral.sh/uv/)
- Bash 工具需要 PowerShell：PowerShell 7（`pwsh`，优先）或系统自带的
  Windows PowerShell 5.1（`powershell.exe`）

## 安装与配置

```powershell
uv sync
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

依赖清单分三层：`pyproject.toml` 声明直接依赖，`uv.lock` 锁定完整解析结果，
`requirements.txt` 是导出给普通 `pip` 环境用的清单
（`pip install -r requirements.txt`）。`requirements.txt` 是生成文件，需要更新时
请重新导出而不是手工修改：`uv export --no-dev --no-hashes --format requirements-txt`。

然后在项目目录编辑 `.env`：

1. 若项目里还没有 `.env`，执行 `Copy-Item .env.example .env`；已有 `.env` 时不要
   覆盖，直接 `notepad .env` 补齐需要的配置项。`.env.example` 里的
   `your-*@example.com`、`your-deepseek-api-key` 等都是占位值，必须替换成真实值；
   把某一项留空表示不启用对应功能（空字符串会被当作未配置）。
2. 启用 DeepSeek 时填写 `DEEPSEEK_API_KEY` 和 `DEEPSEEK_MODEL`；请求超时与重试
   可通过 `DEEPSEEK_TIMEOUT`（秒，默认 30）和 `DEEPSEEK_MAX_RETRIES`
   （默认 2，最大 10）调整。`AGENT_MAX_TOOL_CALLS`（默认 16，取值 1–32）限制单次
   Agent 运行的工具执行次数。
3. 启用联网搜索时，在 [Tavily 官网](https://app.tavily.com/home) 注册并创建
   API key，把 key 填入 `MCP_SEARCH_API_KEY`。`MCP_SEARCH_SERVER_URL` 默认已是
   Tavily 官方托管 MCP endpoint `https://mcp.tavily.com/mcp`，只有换用其他兼容
   服务时才需要改；`MCP_SEARCH_TIMEOUT` 默认 20 秒。不要把 key 放进 URL、代码
   或聊天记录。
4. 启用邮件时填写 `SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASSWORD` 和
   `SMTP_FROM`。默认走 `587` + STARTTLS；使用 `465` 隐式 TLS 时把
   `SMTP_USE_SSL=true`。邮件凭据可以先保留示例占位值，不影响其它功能启动，真正
   发信前再替换成真实值。
5. 本地路径默认值是 `APP_TIMEZONE=Asia/Shanghai`、`REPORT_ROOT=data/reports`、
   `SOURCE_ARCHIVE_ROOT=data/source_archive`、`TEMP_ROOT=data/tmp` 和
   `SQLITE_PATH=data/agentnews.sqlite3`，可以先保留示例值。默认的 FastAPI 应用
   在创建时会自动建立并迁移该 SQLite 文件；需要换位置时改 `SQLITE_PATH`。
6. 运行 `uv sync` 和 `uv run pytest`。不要把 `.env` 提交到 Git。

配置从 `.env` 读取，进程环境变量优先覆盖它。配置加载阶段不校验凭据，所以没有真实
API key 也能启动并加载本地配置；没填（留空）的项会被视为未配置，而留在示例里的
占位值会在真正调用外部服务时以鉴权失败告终。

## 运行

```powershell
uv run agentnews
```

该命令会初始化 `SQLITE_PATH` 指向的 SQLite 数据库、执行所有未应用的迁移，然后
报告命令行入口就绪。

启动本地 FastAPI 服务：

```powershell
uv run uvicorn agentnews.api:app --reload
```

API 启动不依赖任何外部凭据。未配置模型或搜索服务时，对话和日报接口会返回
明确的安全错误，而不是崩溃。

## 日报存储

Markdown 日报存放在 `REPORT_ROOT`（默认 `data/reports`）下的
`YYYY-MM-DD/<report-id>-<slug>.md`。存储层返回的路径相对于该根目录，可以直接
写进 SQLite 的日报索引。相同路径与相同内容重复保存是幂等的；同一路径内容不同
则被拒绝。不同日报 ID 的相同内容会各自存一份，并具有相同的 SHA-256 内容哈希。

## 数据库与迁移

`src/agentnews/db/migrations.py` 就是迁移脚本：六个带版本号的幂等迁移按版本升序
执行，每个迁移各自一个事务，执行后把版本记录进 `schema_migrations`。命令行入口
和 FastAPI 启动都会调用 `initialize_database`，所以手工准备一个数据库只需
`uv run agentnews`。

`docs/schema.sql` 是在空库上完整执行所有迁移后导出的 `CREATE TABLE` /
`CREATE INDEX` 语句，仅作为可读参考；schema 的权威来源始终是 `migrations.py`。

全部迁移执行后的表：`user_preferences`、`reports`、`report_sources`、
`chat_sessions`、`chat_messages`、`email_deliveries`、`email_delivery_attempts`、
`scheduled_digest_runs`、`schema_migrations`。

## 提示词

场景提示词放在 `src/agentnews/agent/prompts/<scene>/vN.md`，由
`prompt_catalog.py` 加载。目前注册了五个场景：`interaction`、`web_research`、
`digest_system`、`digest_markdown` 和 `source_archive`。`v2` 是当前默认版本，
`v1` 继续保留用于回滚与对比。

`interaction` 定义对话风格：结论先行、克制使用 Markdown、只引用真实来源链接、
不重复复述刚生成的日报。它也定义工具选择策略：先用结构化工具，专用工具做不到
时再用经过人工审核的 `bash`（下载图片与文件、运行 CLI、格式转换、多步 shell）。

日报服务把 `digest_system`、`web_research`、`source_archive` 和
`digest_markdown` 拼成一条 system 消息，因此日报的格式契约（一级标题、不要前言、
不要过程说明、不要自建来源小节）集中描述在一处。

助手消息与日报使用同一个安全渲染器（`static/markdown-render.js`）渲染 Markdown，
用户消息保持原文。日报侧，`DigestService` 会剥掉 Agent 前言与过程小节，只保留
正文实际引用的来源（没有引用时回退到抓取到正文的来源），并在末尾统一追加一份
`## 参考来源` 列表。

## 测试

```powershell
uv run pytest
```

测试不需要真实凭据，也不会真的发邮件，所有外部服务都由 fake 或 stub 替代。
当平台不允许在无提权的情况下创建符号链接时（Windows 上的常见情况），相关用例
会被跳过。前端脚本也可以单独运行：
`node tests/agentnews/test_frontend_app.mjs`。

## 项目结构

- `src/agentnews/agent/models/`：LangChain 模型集成。
- `src/agentnews/agent/runner.py`：异步与同步的 LangChain 工具调用循环。
- `src/agentnews/tools/bash.py`：项目自研的 PowerShell 执行器与参数 schema。
- `src/agentnews/services/bash.py`：审核状态机、审计事件与日报应用动作。
- `src/agentnews/tools/digest.py`：每次调用生成一份日报的 `generate_digest` 工具。
- `src/agentnews/tools/mcp_client.py`：异步 MCP 客户端，发现并调用服务端发布的全部工具。
- `src/agentnews/tools/tavily_mcp.py`：面向旧版 Tavily 搜索接口的兼容适配器。
- `src/agentnews/repositories/`：日报文件存储与持久化适配器。
- `src/agentnews/tools/` 与 `src/agentnews/db/`：本地工具与 SQLite 基础设施。
- `src/agentnews/db/migrations.py`：带版本号的迁移脚本；`docs/schema.sql` 是同一
  schema 的生成快照。
- `src/agentnews/main.py`：命令行入口；`config.py` 负责加载配置。
- `tests/agentnews/`：与 `src/agentnews/` 结构对应的单元测试。
- `tests/integration/`：跨模块验收测试。
- `data/`：日报、SQLite 数据库等本地运行时数据，生成内容已被 Git 忽略。

## 人工审核的 Bash

Agent 优先使用结构化工具，只有在它们无法完成任务时才回退到
`bash(command, explanation)`——例如下载图片或文件、运行 CLI、转换格式、串联多个
步骤。工具不设命令白名单：浏览器上会展示将要执行的完整命令、模型给出的简短用途
说明、工作目录、解析到的解释器、环境变量继承策略、超时与输出上限。每次调用都
必须由用户批准；被拒绝的结果会作为工具结果返回给模型。

解释器在命令执行时解析：优先 `pwsh`，先在 `PATH` 中查找，再查 PowerShell 7 的
常见安装位置（`Program Files\PowerShell\7`、用户的 `Microsoft\PowerShell\7`
目录、Microsoft Store 别名）。没有安装 `pwsh` 时回退到系统自带的 Windows
PowerShell 5.1（`%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe`）
并记录一条警告。审核弹窗与工具结果都会标明将要或已经使用的解释器，回退是可见的。
两个解释器都找不到时，已批准的那次调用不会执行，而是返回
`powershell_unavailable` 和可操作的提示；进程启动失败则返回
`powershell_start_failed`。

已批准的命令通过 `-NoLogo -NoProfile -NonInteractive -Command` 在项目目录下运行，
沿用当前进程的环境变量与当前 Windows 用户权限，不会申请管理员提权。命令可以访问
文件、环境变量和网络，因此本应用面向单机单个可信用户，不得暴露到公网。

抓取到的输出先按 UTF-8 解码，失败再回退到系统代码页，因此 Windows PowerShell 5.1
在非 UTF-8 控制台代码页（例如 cp936）下的输出不会乱码。字节上限与超时都作用在
原始输出字节上。

工具还会识别这些经过审计的应用动作：

```text
agentnews reports list
agentnews reports read --id <id>
agentnews reports search --keyword <text>
agentnews reports delete --id <id>
agentnews temp cleanup
```

日报删除动作走 `ReportService`，因此日报索引、Markdown 文件和来源归档会一起清理。
原始 PowerShell 命令在设计上仍可绕过该服务；需要保证日报一致性时请使用应用动作。
每条命令有 60 秒超时、不接 stdin、合并输出上限 1 MiB。待审核请求在内存中保留
五分钟，应用重启后丢弃。

## 每日自动日报

自动发送默认关闭，需要在设置弹窗里打开「每日自动发送」并选择发送时间。开启前会
先做校验，任一条件不满足直接返回 422 而不是等到执行时才失败：
收件邮箱缺失或格式错误返回 `recipient_not_configured` / `invalid_recipient`，
SMTP 配置不完整返回 `smtp_not_configured`，没有配置任何主题或关键词返回
`invalid_digest_preferences`。

调度规则：

- 调度器运行在应用进程内（`uvicorn` 或任何长驻进程），每 30 秒轮询一次。进程
  停止期间不会生成任何日报；`uv run agentnews` 只准备数据库，不会启动调度。
- 触发条件是「保存时区的本地时间到达 `daily_send_time`」且该业务日期还没有运行
  记录。
- 每个业务日期只有一条运行记录：`scheduled_digest_runs.business_date` 有 UNIQUE
  约束，执行前先 claim 该行，因此重复轮询或多个进程也只会真正生成一份日报。
- 临时错误最多重试三次；永久错误（配置无效、没有可靠来源、归档冲突）立即失败。
- 进程停机期间错过当天计划的日期只记一条 `skipped` + `schedule_missed`，不补跑、
  不补发，避免重启时突然发出一份过期日报。
- 邮件按业务日期幂等：计划任务用 `scheduled:<业务日期>`，手动发送用
  `report:<id>:recipient:<地址>:default`，重复点击不会重复投递。

最近一次计划任务的状态可以从 `GET /api/settings` 的 `recent_scheduled_run` 字段
读到（状态、尝试次数、`report_id`、`email_delivery_id` 与错误码），设置弹窗里也会
展示。

## 对话中生成日报

普通对话把 `generate_digest(topic, keyword)` 作为结构化工具暴露给模型。每次调用会
完整走一遍 `DigestService` 的搜索、抓取、Markdown 生成、来源归档和数据库索引流程，
只产出一份日报。请求需要一份还是多份、每次传入什么参数，都由模型自己决定；应用
层不解析自然语言，也不规定固定份数或固定顺序。日报生成不使用 Bash。
