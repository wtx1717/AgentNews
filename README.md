# AgentNews

AgentNews is a personal AI news digest agent. This repository currently contains
the Python project baseline and a minimal command-line entry point; agent,
search, report, API, and email features are not implemented yet.

## Requirements

- Python 3.14 or later
- [uv](https://docs.astral.sh/uv/)

## Setup

```powershell
uv sync
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Edit `.env` in the project directory. Fill in `DEEPSEEK_API_KEY` and
`DEEPSEEK_MODEL` when enabling DeepSeek. Search and email credentials can stay
empty until those integrations are used. The local defaults are
`APP_TIMEZONE=Asia/Shanghai`, `REPORT_ROOT=data/reports`, and
`SQLITE_PATH=data/agentnews.sqlite3`.

Configuration is loaded from `.env` and overridden by process environment
variables. Missing external-service credentials are checked when that service
is initialized, so the application can load local settings without API keys.
Never commit `.env` or paste credentials into chat; `.env` is ignored by Git.

### 配置步骤（Windows PowerShell）

1. 若项目目录中还没有 `.env`，执行 `Copy-Item .env.example .env`；已有 `.env` 时不要覆盖，直接执行 `notepad .env` 并补齐需要的配置项。
2. 启用 DeepSeek 时填写 `DEEPSEEK_API_KEY` 和 `DEEPSEEK_MODEL`。
3. 启用在线搜索时填写 `MCP_SEARCH_SERVER_URL`；是否填写
   `MCP_SEARCH_API_KEY` 取决于所选 MCP 服务。
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

## Test

```powershell
uv run pytest
```

## Project layout

- `src/agentnews/`: application package and command-line entry point.
- `tests/`: automated tests.
- `data/`: local runtime data such as reports and the SQLite database. Generated
  contents are ignored by Git.
