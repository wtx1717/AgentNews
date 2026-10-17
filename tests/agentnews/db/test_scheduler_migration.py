from agentnews.db import connect_database
from agentnews.db.migrations import MIGRATIONS, run_migrations
from agentnews.repositories import SQLiteRepository, UserPreferences


def test_scheduler_migration_adds_defaults_and_is_idempotent(tmp_path):
    database_path = tmp_path / "scheduler.sqlite3"
    connection = connect_database(database_path)
    try:
        assert run_migrations(connection, MIGRATIONS) == [1, 2, 3, 4, 5, 6]
        preferences = connection.execute(
            "SELECT daily_send_enabled, daily_send_time FROM user_preferences WHERE id = 1"
        ).fetchone()
        assert tuple(preferences) == (0, "08:00")
        assert run_migrations(connection, MIGRATIONS) == []
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(scheduled_digest_runs)")
        }
        assert {"business_date", "status", "attempt_count", "report_id", "email_delivery_id"} <= columns
    finally:
        connection.close()


def test_scheduler_preferences_persist_in_sqlite(tmp_path):
    repository = SQLiteRepository(tmp_path / "scheduler.sqlite3")
    try:
        saved = repository.save_preferences(
            UserPreferences(
                email="user@example.test",
                timezone="Europe/Berlin",
                topics=["AI"],
                daily_send_enabled=True,
                daily_send_time="07:05",
            )
        )
    finally:
        repository.close()

    restarted = SQLiteRepository(tmp_path / "scheduler.sqlite3")
    try:
        assert restarted.get_preferences() == saved
    finally:
        restarted.close()

