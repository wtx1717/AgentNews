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
```

## Run

```powershell
uv run agentnews
```

## Test

```powershell
uv run pytest
```

## Project layout

- `src/agentnews/`: application package and command-line entry point.
- `tests/`: automated tests.
- `data/`: local runtime data such as reports and the SQLite database. Generated
  contents are ignored by Git.
