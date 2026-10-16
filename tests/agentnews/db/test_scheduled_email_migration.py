"""Regression tests for the scheduled-task trigger source CHECK constraint.

Databases created before migration 4 allowed ``scheduled_task`` kept
``CHECK (trigger_source IN ('manual_api', 'natural_language'))``. Every scheduled
email insert then raised ``sqlite3.IntegrityError``, which the scheduler reported as
the generic ``scheduler_error`` after exhausting its retries, and the run never got an
``email_delivery_id``. Migration 6 rebuilds the table to widen the constraint.
"""

from __future__ import annotations

from datetime import date
import sqlite3

from agentnews.db.connection import connect_database
from agentnews.db.migrations import MIGRATIONS, run_migrations
from agentnews.repositories import (
    ReportStorage,
    SQLiteRepository,
    UserPreferences,
)

# The exact legacy statement that produced the broken constraint in the wild: the
# ``trigger_source`` column was added by an ALTER TABLE whose CHECK listed only the
# two pre-scheduler sources.
_LEGACY_EMAIL_DELIVERIES = """
CREATE TABLE email_deliveries (
    id INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sent', 'failed')),
    error_message TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    trigger_source TEXT NOT NULL DEFAULT 'manual_api'
        CHECK (trigger_source IN ('manual_api', 'natural_language')),
    idempotency_key TEXT,
    error_code TEXT,
    FOREIGN KEY (report_id) REFERENCES reports(id) ON DELETE CASCADE
)
"""

_LEGACY_IDEMPOTENCY_INDEX = """
CREATE UNIQUE INDEX idx_email_deliveries_idempotency_key
    ON email_deliveries(idempotency_key)
    WHERE idempotency_key IS NOT NULL
"""

_LEGACY_DELIVERY_INSERT = """
INSERT INTO email_deliveries
    (report_id, recipient, subject, status, error_message, retry_count,
     requested_at, completed_at, trigger_source, idempotency_key, error_code)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _table_sql(connection) -> str:
    return connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'email_deliveries'"
    ).fetchone()["sql"]


def _legacy_database(tmp_path, *, with_row: bool = False):
    """Build the on-disk state a pre-fix installation was left in.

    Migration 1 already creates ``email_deliveries`` without ``trigger_source``; the
    broken shape is the result of an early version of migration 4 adding that column
    with a two-value CHECK. Reproduce it by replacing the table.
    """

    database_path = tmp_path / "legacy-scheduled-email.sqlite3"
    connection = connect_database(database_path)
    assert run_migrations(connection, MIGRATIONS[:3]) == [1, 2, 3]
    connection.execute("DROP TABLE email_deliveries")
    connection.execute(_LEGACY_EMAIL_DELIVERIES)
    connection.execute(_LEGACY_IDEMPOTENCY_INDEX)
    connection.execute(
        "INSERT INTO reports (id, report_date, title, file_path, content_hash) "
        "VALUES (7, '2026-10-15', '旧日报', '2026-10-15/7-旧日报.md', 'old-hash')"
    )
    if with_row:
        connection.execute(
            _LEGACY_DELIVERY_INSERT,
            (
                7,
                "user@example.test",
                "旧邮件",
                "sent",
                None,
                0,
                "2026-10-15T09:00:00+00:00",
                "2026-10-15T09:00:01+00:00",
                "natural_language",
                "report:7:recipient:user@example.test:default",
                None,
            ),
        )
    connection.commit()
    return database_path, connection


def test_legacy_constraint_rejects_scheduled_task_before_migration(tmp_path):
    """Guard the premise: the historical schema really is what broke the scheduler."""

    database_path, connection = _legacy_database(tmp_path)
    try:
        assert "scheduled_task" not in _table_sql(connection)
        try:
            connection.execute(
                _LEGACY_DELIVERY_INSERT,
                (
                    7,
                    "user@example.test",
                    "日报",
                    "pending",
                    None,
                    0,
                    "2026-10-16T12:05:00+00:00",
                    None,
                    "scheduled_task",
                    "scheduled:2026-10-16",
                    None,
                ),
            )
            raise AssertionError("legacy schema unexpectedly accepted scheduled_task")
        except sqlite3.IntegrityError as error:
            assert "trigger_source" in str(error)
    finally:
        connection.close()
    assert database_path.exists()


def test_scheduled_task_source_migration_accepts_scheduled_task(tmp_path):
    database_path, connection = _legacy_database(tmp_path)
    try:
        assert run_migrations(connection, MIGRATIONS) == [4, 5, 6]
        assert "scheduled_task" in _table_sql(connection)
        connection.execute(
            _LEGACY_DELIVERY_INSERT,
            (
                7,
                "user@example.test",
                "日报",
                "pending",
                None,
                0,
                "2026-10-16T12:05:00+00:00",
                None,
                "scheduled_task",
                "scheduled:2026-10-16",
                None,
            ),
        )
        connection.commit()
        stored = connection.execute(
            "SELECT trigger_source, idempotency_key FROM email_deliveries "
            "WHERE idempotency_key = 'scheduled:2026-10-16'"
        ).fetchone()
        assert stored["trigger_source"] == "scheduled_task"
    finally:
        connection.close()
    assert database_path.exists()


def test_scheduled_task_source_migration_preserves_rows_and_other_sources(tmp_path):
    database_path, connection = _legacy_database(tmp_path, with_row=True)
    try:
        assert run_migrations(connection, MIGRATIONS) == [4, 5, 6]
        preserved = connection.execute(
            "SELECT * FROM email_deliveries WHERE report_id = 7"
        ).fetchone()
        assert preserved["recipient"] == "user@example.test"
        assert preserved["subject"] == "旧邮件"
        assert preserved["status"] == "sent"
        assert preserved["trigger_source"] == "natural_language"
        assert (
            preserved["idempotency_key"]
            == "report:7:recipient:user@example.test:default"
        )
        assert preserved["completed_at"] == "2026-10-15T09:00:01+00:00"

        # The unique idempotency index must survive the table rebuild.
        connection.execute(
            "INSERT INTO email_deliveries (report_id, recipient, subject, trigger_source, "
            "idempotency_key) VALUES (7, 'a@example.test', 's', 'manual_api', 'dup-key')"
        )
        try:
            connection.execute(
                "INSERT INTO email_deliveries (report_id, recipient, subject, trigger_source, "
                "idempotency_key) VALUES (7, 'b@example.test', 's', 'scheduled_task', 'dup-key')"
            )
            raise AssertionError("idempotency key uniqueness was lost in the rebuild")
        except sqlite3.IntegrityError:
            connection.rollback()
    finally:
        connection.close()
    assert database_path.exists()


def test_scheduled_task_source_migration_keeps_cascade_delete(tmp_path):
    database_path, connection = _legacy_database(tmp_path, with_row=True)
    try:
        assert run_migrations(connection, MIGRATIONS) == [4, 5, 6]
        connection.commit()
        connection.execute("DELETE FROM reports WHERE id = 7")
        connection.commit()
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM email_deliveries WHERE report_id = 7"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()
    assert database_path.exists()


def test_scheduled_task_source_migration_is_idempotent(tmp_path):
    database_path, connection = _legacy_database(tmp_path)
    try:
        assert run_migrations(connection, MIGRATIONS) == [4, 5, 6]
        assert run_migrations(connection, MIGRATIONS) == []
        assert run_migrations(connection, MIGRATIONS) == []
        assert "scheduled_task" in _table_sql(connection)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM email_deliveries WHERE trigger_source = 'scheduled_task'"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()
    assert database_path.exists()


def test_scheduled_email_send_succeeds_on_repository_restart(tmp_path):
    """End-to-end guard: the scheduled trigger source must be insertable in SQLite.

    This is the regression that the in-memory repository could never catch, because
    it accepts ``scheduled_task`` without any constraint behind it.
    """

    database_path, connection = _legacy_database(tmp_path)
    try:
        assert run_migrations(connection, MIGRATIONS) == [4, 5, 6]
    finally:
        connection.close()

    repository = SQLiteRepository(database_path)
    try:
        repository.save_preferences(
            UserPreferences(email="user@example.test", topics=["AI"])
        )
        storage = ReportStorage(tmp_path / "reports")
        reservation = repository.reserve_report(date(2026, 10, 16), "2026-10-16 新闻日报")
        stored = storage.save_report(
            reservation.id,
            reservation.report_date,
            reservation.title,
            "# 日报\n",
        )
        repository.finalize_report(
            reservation.id, stored.relative_path, "摘要", stored.content_hash
        )

        delivery, created = repository.reserve_email_delivery(
            reservation.id,
            "user@example.test",
            "AgentNews 日报 | 2026-10-16 | 2026-10-16 新闻日报",
            "scheduled_task",
            "scheduled:2026-10-16",
        )
        assert created is True
        assert delivery.trigger_source == "scheduled_task"
        assert delivery.status == "pending"
        assert delivery.error_code is None
    finally:
        repository.close()
    assert database_path.exists()
