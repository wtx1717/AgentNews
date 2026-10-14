# AgentNews

AgentNews is a personal AI news digest agent. The repository includes the
Python project baseline, SQLite migrations, local file tools, a Tavily MCP
search adapter, Markdown report storage, and a minimal command-line entry point.
Agent orchestration, report generation, API, and email features are not
implemented yet.

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
The Tavily search URL is preconfigured in `.env.example`; add a Tavily API key
when enabling online search. Search timeout defaults to 20 seconds. Email
credentials can stay empty until that integration is used.
The local defaults are `APP_TIMEZONE=Asia/Shanghai`, `REPORT_ROOT=data/reports`,
and `SQLITE_PATH=data/agentnews.sqlite3`.

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
   `SMTP_PASSWORD` 和 `SMTP_FROM`。默认端口为 `587`。
5. `APP_TIMEZONE`、`REPORT_ROOT` 和 `SQLITE_PATH` 可先保留示例默认值。
6. 运行 `uv sync` 和 `uv run pytest`。不要把 `.env` 提交到 Git。

## Run

```powershell
uv run agentnews
```

The command initializes the SQLite database at `SQLITE_PATH` and applies any
pending migrations before reporting that the CLI is ready.

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
- `src/agentnews/tools/tavily_mcp.py`: async Tavily MCP search adapter.
- `src/agentnews/repositories/`: report file storage and persistence adapters.
- `src/agentnews/tools/` and `src/agentnews/db/`: local tools and SQLite infrastructure.
- `src/agentnews/main.py`: command-line entry point; `config.py` loads settings.
- `tests/`: automated tests.
- `data/`: local runtime data such as reports and the SQLite database. Generated
  contents are ignored by Git.
