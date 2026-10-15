import sqlite3

import pytest

from agentnews.db import connect_database, initialize_database, transaction
from agentnews.db.migrations import Migration, run_migrations


EXPECTED_TABLES = {
    "schema_migrations",
    "user_preferences",
    "reports",
    "report_sources",
    "chat_sessions",
    "chat_messages",
    "email_deliveries",
}


def table_names(connection):
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row["name"] for row in rows}


def test_empty_database_initializes_schema_and_default_preferences(tmp_path):
    database_path = tmp_path / "nested" / "agentnews.sqlite3"

    assert initialize_database(database_path) == [1, 2]

    connection = connect_database(database_path)
    try:
        assert EXPECTED_TABLES <= table_names(connection)
        preference = connection.execute(
            "SELECT id, timezone, topics_json, keywords_json FROM user_preferences"
        ).fetchone()
        assert dict(preference) == {
            "id": 1,
            "timezone": "Asia/Shanghai",
            "topics_json": "[]",
            "keywords_json": "[]",
        }
        migration = connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchone()
        assert (migration["version"], migration["name"]) == (1, "initial_schema")
        assert connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()["version"] == 2
        assert "archive_file_path" in {
            row["name"] for row in connection.execute("PRAGMA table_info(report_sources)")
        }
    finally:
        connection.close()


def test_repeated_initialization_is_idempotent(tmp_path):
    database_path = tmp_path / "agentnews.sqlite3"

    assert initialize_database(database_path) == [1, 2]
    assert initialize_database(database_path) == []

    connection = connect_database(database_path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM schema_migrations"
        ).fetchone()["count"] == 2
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM user_preferences"
        ).fetchone()["count"] == 1
    finally:
        connection.close()


def test_v1_database_upgrades_without_losing_rows(tmp_path):
    database_path = tmp_path / "v1.sqlite3"
    connection = connect_database(database_path)
    try:
        connection.execute(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "CREATE TABLE user_preferences ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), display_name TEXT, email TEXT, "
            "timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai', topics_json TEXT NOT NULL DEFAULT '[]', "
            "keywords_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.execute("INSERT INTO user_preferences (id, display_name) VALUES (1, '保留')")
        connection.execute(
            "CREATE TABLE reports (id INTEGER PRIMARY KEY, report_date TEXT NOT NULL, "
            "title TEXT NOT NULL, file_path TEXT NOT NULL UNIQUE, summary TEXT NOT NULL DEFAULT '', "
            "content_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "CREATE TABLE report_sources (id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL, "
            "title TEXT NOT NULL, url TEXT NOT NULL, source_name TEXT NOT NULL, published_at TEXT, "
            "snippet TEXT NOT NULL DEFAULT '', FOREIGN KEY (report_id) REFERENCES reports(id) ON DELETE CASCADE)"
        )
        connection.execute(
            "INSERT INTO reports (id, report_date, title, file_path, content_hash) "
            "VALUES (7, '2026-10-14', '日报', 'daily.md', 'hash')"
        )
        connection.execute(
            "INSERT INTO report_sources (report_id, title, url, source_name) "
            "VALUES (7, '来源', 'https://example.test', 'Example')"
        )
        connection.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (1, 'initial_schema')"
        )
    finally:
        connection.close()

    assert initialize_database(database_path) == [2]
    upgraded = connect_database(database_path)
    try:
        assert upgraded.execute(
            "SELECT display_name FROM user_preferences WHERE id = 1"
        ).fetchone()["display_name"] == "保留"
        assert upgraded.execute("SELECT title FROM reports WHERE id = 7").fetchone()["title"] == "日报"
        assert upgraded.execute(
            "SELECT COUNT(*) AS count FROM report_sources WHERE report_id = 7"
        ).fetchone()["count"] == 1
        assert "archive_file_path" in {
            row["name"] for row in upgraded.execute("PRAGMA table_info(report_sources)")
        }
    finally:
        upgraded.close()


def test_transaction_rolls_back_data_on_exception(tmp_path):
    connection = connect_database(tmp_path / "agentnews.sqlite3")
    try:
        connection.execute("CREATE TABLE values_table (value TEXT NOT NULL)")
        with pytest.raises(RuntimeError):
            with transaction(connection):
                connection.execute("INSERT INTO values_table VALUES ('discarded')")
                raise RuntimeError("force rollback")

        assert connection.execute("SELECT COUNT(*) FROM values_table").fetchone()[0] == 0
    finally:
        connection.close()


def test_transaction_rolls_back_when_commit_fails(tmp_path):
    connection = connect_database(tmp_path / "agentnews.sqlite3")
    try:
        connection.execute("CREATE TABLE parents (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE children (parent_id INTEGER REFERENCES parents(id) "
            "DEFERRABLE INITIALLY DEFERRED)"
        )

        with pytest.raises(sqlite3.IntegrityError):
            with transaction(connection):
                connection.execute("INSERT INTO children VALUES (99)")

        assert not connection.in_transaction
        assert connection.execute("SELECT COUNT(*) FROM children").fetchone()[0] == 0
    finally:
        connection.close()


def test_failed_migration_rolls_back_ddl_and_version_record(tmp_path):
    database_path = tmp_path / "agentnews.sqlite3"

    def failing_migration(connection):
        connection.execute("CREATE TABLE should_be_rolled_back (id INTEGER)")
        raise RuntimeError("migration failed")

    failing = (Migration(1, "failing", failing_migration),)
    connection = connect_database(database_path)
    try:
        with pytest.raises(RuntimeError, match="migration failed"):
            run_migrations(connection, failing)

        assert "should_be_rolled_back" not in table_names(connection)
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0
    finally:
        connection.close()


def test_foreign_keys_are_enabled_and_cascade_deletes(tmp_path):
    database_path = tmp_path / "agentnews.sqlite3"
    initialize_database(database_path)
    connection = connect_database(database_path)
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        connection.execute(
            "INSERT INTO reports (report_date, title, file_path, content_hash) "
            "VALUES ('2026-10-14', 'Test', 'reports/test.md', 'hash')"
        )
        report_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(
            "INSERT INTO report_sources (report_id, title, url, source_name) "
            "VALUES (?, 'Source', 'https://example.com', 'Example')",
            (report_id,),
        )
        connection.execute("DELETE FROM reports WHERE id = ?", (report_id,))
        assert connection.execute(
            "SELECT COUNT(*) FROM report_sources WHERE report_id = ?", (report_id,)
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_connection_has_row_factory_and_busy_timeout(tmp_path):
    connection = connect_database(tmp_path / "agentnews.sqlite3")
    try:
        assert connection.row_factory is sqlite3.Row
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        connection.close()
