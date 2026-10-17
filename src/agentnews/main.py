"""Command-line entry point for AgentNews."""

from .config import Settings, get_settings
from .db import initialize_database


def main(settings: Settings | None = None) -> None:
    """初始化数据库并打印命令行脚手架就绪提示。"""
    settings = settings or get_settings()
    initialize_database(settings.sqlite_path)
    print("AgentNews command-line scaffold is ready.")
