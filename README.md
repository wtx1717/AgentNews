# AgentNews

AgentNews is a personal AI news digest agent. The repository includes the
Python project baseline, SQLite migrations, local file tools, a Tavily MCP
search adapter, Markdown report storage, and a minimal command-line entry point.
LangChain Agent tool orchestration is included. The Agent discovers tools
published by the configured MCP service and registers those tools alongside the
four managed local file tools. It also has a project-owned
`bash(command, explanation)` tool: every call pauses for local human approval
before a PowerShell 7 process runs with the current Windows user's permissions.
The FastAPI backend exposes chat, settings, one-off digest, report, source,
and health endpoints. It uses SQLite for business persistence by default;
explicitly passing `InMemoryRepository` remains available for tests and local
integration scenarios.

## Requirements

- Python 3.14 or later
- [uv](https://docs.astral.sh/uv/)

## Setup

```powershell
uv sync
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Edit `.env` in the project directory. Fill in `DEEPSEEK_API_KEY` and
`DEEPSEEK_MODEL` when enabling DeepSeek. `DEEPSEEK_TIMEOUT` (seconds, default
30) and `DEEPSEEK_MAX_RETRIES` (default 2, maximum 10) control model requests.
`AGENT_MAX_TOOL_CALLS` (default 8, range 1-32) limits tool executions per run.
The Tavily search URL is preconfigured in `.env.example`; add a Tavily API key
when enabling online search. Search timeout defaults to 20 seconds. Email
credentials can stay empty until that integration is used.
The local defaults are `APP_TIMEZONE=Asia/Shanghai`, `REPORT_ROOT=data/reports`,
`TEMP_ROOT=data/tmp`, and `SQLITE_PATH=data/agentnews.sqlite3`. The default FastAPI application
creates and migrates that SQLite file when the application is created; set
`SQLITE_PATH` to choose a different persistence location.

Configuration is loaded from `.env` and overridden by process environment
variables. Missing external-service credentials are checked when that service
is initialized, so the application can load local settings without API keys.
Never commit `.env` or paste credentials into chat; `.env` is ignored by Git.

### 配置步骤（Windows PowerShell）

1. 若项目目录中还没有 `.env`，执行 `Copy-Item .env.example .env`；已有 `.env` 时不要覆盖，直接执行 `notepad .env` 并补齐需要的配置项。
2. 启用 DeepSeek 时填写 `DEEPSEEK_API_KEY` 和 `DEEPSEEK_MODEL`；超时和重试次数可通过 `DEEPSEEK_TIMEOUT`、`DEEPSEEK_MAX_RETRIES` 调整。
3. 在 [Tavily 官网](https://app.tavily.com/home) 注册并创建 API key，然后填写
   `MCP_SEARCH_API_KEY`。`MCP_SEARCH_SERVER_URL` 默认是 Tavily 官方托管 MCP
   endpoint `https://mcp.tavily.com/mcp`，只有使用其他兼容服务时才需要改；
   `MCP_SEARCH_TIMEOUT` 默认为 20 秒。不要把 key 放入 URL、代码或聊天。
4. 启用邮件时填写 `SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、
   `SMTP_PASSWORD` 和 `SMTP_FROM`。默认使用 `587` + STARTTLS；使用
   `465` 隐式 TLS 时，将 `SMTP_USE_SSL=true`。
5. `APP_TIMEZONE`、`REPORT_ROOT` 和 `SQLITE_PATH` 可先保留示例默认值。
6. 运行 `uv sync` 和 `uv run pytest`。不要把 `.env` 提交到 Git。

## Run

```powershell
uv run agentnews
```

The command initializes the SQLite database at `SQLITE_PATH` and applies any
pending migrations before reporting that the CLI is ready.

To run the local FastAPI API:

```powershell
uv run uvicorn agentnews.api:app --reload
```

The API does not require external credentials to start. Chat and digest calls
return a safe error until the corresponding model or search configuration is
provided.

## Report storage

Markdown reports are stored beneath `REPORT_ROOT` (default: `data/reports`) in
`YYYY-MM-DD/<report-id>-<slug>.md` paths. Paths returned by the report storage
layer are relative to that root and can be stored in the SQLite report index.
Saving the same report path and content is idempotent; different content at an
existing report path is rejected. Identical content for different report IDs is
stored separately and has the same SHA-256 content hash.

## Test

```powershell
uv run pytest
```

## Project layout

- `src/agentnews/agent/models/`: LangChain model integrations.
- `src/agentnews/agent/runner.py`: async and sync LangChain tool-calling runner.
- `src/agentnews/tools/bash.py`: project-owned PowerShell executor and schema.
- `src/agentnews/services/bash.py`: approval state machine, audit events, and report actions.
- `src/agentnews/tools/digest.py`: the structured `generate_digest` tool for one digest per call.
- `src/agentnews/tools/mcp_client.py`: async MCP client that discovers and invokes every published service tool.
- `src/agentnews/tools/tavily_mcp.py`: compatibility adapter for the legacy Tavily search API.
- `src/agentnews/repositories/`: report file storage and persistence adapters.
- `src/agentnews/tools/` and `src/agentnews/db/`: local tools and SQLite infrastructure.
- `src/agentnews/main.py`: command-line entry point; `config.py` loads settings.
- `tests/agentnews/`: unit tests organized to mirror the `src/agentnews/` package.
- `tests/integration/`: cross-module acceptance tests.
- `data/`: local runtime data such as reports and the SQLite database. Generated
  contents are ignored by Git.

## Human-approved Bash

The Agent may request `bash(command, explanation)` when the registered
structured tools cannot complete an explicitly requested task. The browser
shows the exact command, the model's short purpose statement, the working
directory, inherited-environment policy, timeout, and output limit. A user
must approve every call; a rejection is returned to the model as a tool result.

Approved commands run through `pwsh -NoLogo -NoProfile -NonInteractive` in the
project directory, with the current process environment and current Windows
user permissions. The tool does not request administrator elevation. Commands
can access files, environment variables, and the network, so this application is
intended for one trusted user on the local machine and must not be exposed to
the public internet.

The tool also recognizes these audited application actions:

```text
agentnews reports list
agentnews reports read --id <id>
agentnews reports search --keyword <text>
agentnews reports delete --id <id>
agentnews temp cleanup
```

The report delete action uses `ReportService` so the report index, Markdown
file, and source archives are cleaned together. Raw PowerShell commands remain
fully capable of bypassing that service by design; use the application action
when report consistency is required. Each command has a 60-second timeout, no
stdin, and a 1 MiB combined output limit. Pending approvals are held in memory
for five minutes and are discarded when the application restarts.

## Conversational digest generation

Ordinary chat exposes `generate_digest(topic, keyword)` as a structured tool.
Each call runs the existing `DigestService` search, fetch, Markdown, source
archive, and database-index workflow for one report. The model decides whether
the request needs one call or several calls and supplies the parameters for
each call; the application does not parse natural language or impose a fixed
number or order of reports. Bash is not used for digest generation.
