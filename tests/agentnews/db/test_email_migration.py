from __future__ import annotations

from agentnews.db.connection import connect_database
from agentnews.db.migrations import MIGRATIONS, run_migrations


def test_email_tracking_migration_preserves_existing_delivery_rows(tmp_path):
    connection = connect_database(tmp_path / "email-v3.sqlite3")
    try:
        assert run_migrations(connection, MIGRATIONS[:3]) == [1, 2, 3]
        connection.execute(
            "INSERT INTO reports (id, report_date, title, file_path, content_hash) "
            "VALUES (9, '2026-10-16', '日报', 'daily.md', 'hash')"
        )
        connection.execute(
            """
            INSERT INTO email_deliveries
                (id, report_id, recipient, subject, status, error_message)
            VALUES (4, 9, 'user@example.test', '旧邮件', 'sent', NULL)
            """
        )

        assert run_migrations(connection, MIGRATIONS) == [4, 5]
        assert run_migrations(connection, MIGRATIONS) == []
        delivery = connection.execute(
            "SELECT * FROM email_deliveries WHERE id = 4"
        ).fetchone()
        assert delivery["status"] == "sent"
        assert delivery["trigger_source"] == "manual_api"
        assert delivery["idempotency_key"] is None
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()[-1]["version"] == 5
        connection.execute("DELETE FROM reports WHERE id = 9")
        assert connection.execute(
            "SELECT COUNT(*) FROM email_deliveries WHERE id = 4"
        ).fetchone()[0] == 0
    finally:
        connection.close()
