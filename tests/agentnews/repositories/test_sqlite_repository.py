from datetime import UTC, date

import pytest

from agentnews.db import connect_database
from agentnews.repositories import SQLiteRepository, UserPreferences


def test_preferences_and_messages_survive_restart_with_safe_metadata(tmp_path):
    database_path = tmp_path / "agentnews.sqlite3"
    repository = SQLiteRepository(database_path)
    saved = repository.save_preferences(
        UserPreferences(
            display_name=" 用户 ",
            email=" user@example.test ",
            topics=[" AI ", "AI"],
            keywords=[" Agent "],
        )
    )
    session = repository.create_session("  Chat  ")
    metadata = {
        "tool_name": "search",
        "call_count": 2,
        "stop_reason": "completed",
        "nested": {"error_code": "none"},
    }
    message = repository.append_message(session.id, "tool", "result", metadata)
    metadata["nested"]["error_code"] = "changed"
    repository.close()

    restarted = SQLiteRepository(database_path)
    try:
        assert restarted.get_preferences() == saved
        assert restarted.list_messages(session.id)[0].metadata["nested"]["error_code"] == "none"
        assert restarted.list_messages(session.id)[0].created_at.tzinfo is UTC
        with pytest.raises(ValueError, match="sensitive"):
            restarted.append_message(session.id, "tool", "bad", {"api_key": "secret"})
        assert restarted.list_messages(session.id) == (message,)
    finally:
        restarted.close()

    connection = connect_database(database_path)
    try:
        rows = connection.execute("SELECT metadata_json FROM chat_messages").fetchall()
        assert "secret" not in " ".join(str(row["metadata_json"]) for row in rows)
    finally:
        connection.close()


def test_report_reservation_sources_cancellation_and_id_monotonicity(tmp_path):
    repository = SQLiteRepository(tmp_path / "agentnews.sqlite3")
    try:
        pending = repository.reserve_report(date(2026, 10, 14), "Daily")
        assert repository.list_reports() == ()
        source = repository.add_report_source(
            pending.id,
            "Source",
            "https://example.test/news",
            "Example",
            archive_file_path="2026-10-14/1-source.md",
        )
        assert repository.get_report_sources(pending.id) == ()
        assert repository.add_report_source(
            pending.id,
            "Changed",
            "https://example.test/news",
            "Other",
        ) == source
        report = repository.finalize_report(pending.id, "2026-10-14/1.md", "Summary", "hash")
        assert repository.get_report(report.id) == report
        assert repository.get_report_sources(report.id) == (source,)
        assert repository.get_source(source.id) == source
        assert not repository.cancel_report(report.id)

        cancelled = repository.reserve_report(date(2026, 10, 15), "Cancelled")
        repository.add_report_source(
            cancelled.id,
            "Source",
            "https://example.test/cancelled",
            "Example",
        )
        assert repository.cancel_report(cancelled.id)
        assert not repository.cancel_report(cancelled.id)
        assert repository.get_report(cancelled.id) is None
        assert repository.get_report_sources(cancelled.id) == ()
        with pytest.raises(KeyError):
            repository.finalize_report(cancelled.id, "cancelled.md", "", "hash")
        with pytest.raises(KeyError):
            repository.add_report_source(
                cancelled.id, "Source", "https://example.test/again", "Example"
            )

        next_report = repository.reserve_report(date(2026, 10, 15), "Next")
        assert next_report.id > cancelled.id
    finally:
        repository.close()


def test_report_listing_date_sort_and_pagination(tmp_path):
    repository = SQLiteRepository(tmp_path / "agentnews.sqlite3")
    try:
        for report_date, title in (
            (date(2026, 10, 12), "Old"),
            (date(2026, 10, 14), "First"),
            (date(2026, 10, 14), "Second"),
        ):
            reservation = repository.reserve_report(report_date, title)
            repository.finalize_report(
                reservation.id,
                f"{reservation.id}.md",
                title,
                f"hash-{reservation.id}",
            )
        reports = repository.list_reports(limit=2)
        assert [report.title for report in reports] == ["Second", "First"]
        assert [report.title for report in repository.list_reports(
            start_date=date(2026, 10, 14),
            end_date=date(2026, 10, 14),
            offset=1,
        )] == ["First"]
    finally:
        repository.close()


def test_unknown_session_and_invalid_metadata_are_rejected(tmp_path):
    repository = SQLiteRepository(tmp_path / "agentnews.sqlite3")
    try:
        with pytest.raises(KeyError):
            repository.list_messages("missing")
        with pytest.raises(ValueError):
            repository.append_message("missing", "user", "bad", {"smtp_password": "x"})
        with pytest.raises(KeyError):
            repository.append_message("missing", "user", "bad", {"tool_name": "x"})
    finally:
        repository.close()


def test_foreign_key_cascade_removes_messages_and_sources(tmp_path):
    repository = SQLiteRepository(tmp_path / "agentnews.sqlite3")
    try:
        session = repository.create_session()
        repository.append_message(session.id, "user", "hello")
        reservation = repository.reserve_report(date(2026, 10, 14), "Daily")
        repository.add_report_source(
            reservation.id, "Source", "https://example.test", "Example"
        )
        repository.finalize_report(reservation.id, "daily.md", "", "hash")
        with repository._lock:
            repository._connection.execute("DELETE FROM chat_sessions WHERE id = ?", (session.id,))
            repository._connection.execute("DELETE FROM reports WHERE id = ?", (reservation.id,))
        with pytest.raises(KeyError):
            repository.list_messages(session.id)
        assert repository.get_report_sources(reservation.id) == ()
    finally:
        repository.close()


def test_delete_report_uses_foreign_key_cascade_for_sources(tmp_path):
    repository = SQLiteRepository(tmp_path / "agentnews.sqlite3")
    try:
        reservation = repository.reserve_report(date(2026, 10, 14), "Daily")
        source = repository.add_report_source(
            reservation.id, "Source", "https://example.test", "Example"
        )
        repository.finalize_report(reservation.id, "daily.md", "", "hash")

        assert repository.delete_report(reservation.id)
        assert not repository.delete_report(reservation.id)
        assert repository.get_report(reservation.id) is None
        assert repository.get_report_sources(reservation.id) == ()
        assert repository.get_source(source.id) is None
    finally:
        repository.close()
