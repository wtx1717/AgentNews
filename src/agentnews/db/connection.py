"""SQLite 连接建立、显式事务管理与数据库初始化工具。"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .migrations import Migration


def connect_database(
    path: str | Path,
    *,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Open a configured SQLite connection.

    The returned connection is owned by the caller and must be closed after
    use. Explicit transaction control keeps migration and application writes
    predictable across Python and SQLite versions.
    """

    database_path = Path(path)
    if str(path) != ":memory:":
        database_path.parent.mkdir(parents=True, exist_ok=True)

    # 关闭驱动自动提交，由 transaction() 显式控制事务边界。
    connection = sqlite3.connect(
        str(path) if str(path) == ":memory:" else database_path,
        timeout=5.0,
        isolation_level=None,
        check_same_thread=check_same_thread,
    )
    connection.row_factory = sqlite3.Row
    # 外键约束按连接生效，不设置则默认关闭。
    connection.execute("PRAGMA foreign_keys = ON")
    # 写锁争抢时最多等待 5 秒，避免立即抛 database is locked。
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextmanager
def transaction(
    connection: sqlite3.Connection,
    *,
    immediate: bool = False,
) -> Iterator[sqlite3.Connection]:
    """Run a block in an explicit transaction and roll it back on failure."""

    if connection.in_transaction:
        raise sqlite3.OperationalError("a transaction is already active")

    # IMMEDIATE 提前取写锁，避免事务中途升级锁失败。
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


def initialize_database(
    path: str | Path,
    migrations: tuple["Migration", ...] | None = None,
) -> list[int]:
    """Apply pending migrations and close the database connection."""

    from .migrations import MIGRATIONS, run_migrations

    connection = connect_database(path)
    try:
        return run_migrations(connection, MIGRATIONS if migrations is None else migrations)
    finally:
        connection.close()
