"""Command-line entry point for AgentNews."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from typing import Any

from .config import Settings, get_settings
from .db import initialize_database

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def main(
    settings: Settings | None = None,
    argv: Sequence[str] | None = None,
    *,
    run_server: Callable[..., Any] | None = None,
) -> None:
    """启动本地 Web 服务；`init-db` 子命令只初始化数据库。"""
    parser = argparse.ArgumentParser(
        prog="agentnews",
        description="AgentNews 个人新闻日报 Agent",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("serve", "init-db"),
        default="serve",
        help="serve 启动 Web 服务（默认），init-db 只初始化数据库",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口，默认 8000")
    parser.add_argument("--no-reload", action="store_true", help="关闭代码热重载")
    args = parser.parse_args(argv)

    settings = settings or get_settings()
    initialize_database(settings.sqlite_path)

    if args.command == "init-db":
        print("AgentNews 数据库已就绪。")
        return

    print(f"AgentNews 正在启动：http://{args.host}:{args.port}")
    server = run_server
    if server is None:
        import uvicorn

        server = uvicorn.run
    server(
        "agentnews.api:app",
        host=args.host,
        port=args.port,
        reload=not args.no_reload,
    )
