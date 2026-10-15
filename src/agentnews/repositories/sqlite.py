from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
import json
from pathlib import Path
import sqlite3
import threading
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from ..db.connection import connect_database, transaction
from ..db.migrations import MIGRATIONS, run_migrations
from .protocol import (
    ChatMessage,
    ChatSession,
    MessageRole,
    ReportRecord,
    ReportReservation,
    ReportSource,
    SessionArchivedError,
    SessionStatus,
    UserPreferences,
)


_PLACEHOLDER_PREFIX = "__agentnews_placeholder__/"
_PENDING_PLACEHOLDER_PREFIX = _PLACEHOLDER_PREFIX + "pending/"
_CANCELLED_PLACEHOLDER_PREFIX = _PLACEHOLDER_PREFIX + "cancelled/"
_FORBIDDEN_METADATA_PARTS = (
    "api_key",
    "apikey",
    "api-key",
    "smtp_password",
    "password",
    "secret",
    "credential",
    "authorization",
    "configuration",
    "config",
)


class SQLiteRepository:
    """SQLite-backed implementation of the application repository contract."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self.database_path = database_path
        self._lock = threading.RLock()
        self._owns_connection = connection is None
        self._connection = (
            connection
            if connection is not None
            else connect_database(database_path, check_same_thread=False)
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        run_migrations(self._connection, MIGRATIONS)
        self._closed = False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._owns_connection:
                self._connection.close()

    def get_preferences(self) -> UserPreferences:
        with self._lock:
            row = self._connection_or_raise().execute(
                """
                SELECT display_name, email, timezone, topics_json, keywords_json
                FROM user_preferences
                WHERE id = 1
                """
            ).fetchone()
            if row is None:
                raise RuntimeError("user preferences row is missing")
            return _preferences_from_row(row)

    def save_preferences(self, preferences: UserPreferences) -> UserPreferences:
        normalized = _normalize_preferences(preferences)
        topics_json = _dump_json(list(normalized.topics))
        keywords_json = _dump_json(list(normalized.keywords))
        now = _utc_now_text()
        with self._lock, transaction(self._connection_or_raise(), immediate=True):
            cursor = self._connection.execute(
                """
                UPDATE user_preferences
                SET display_name = ?, email = ?, timezone = ?,
                    topics_json = ?, keywords_json = ?, updated_at = ?
                WHERE id = 1
                """,
                (
                    normalized.display_name,
                    normalized.email,
                    normalized.timezone,
                    topics_json,
                    keywords_json,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("user preferences row is missing")
        return normalized

    def create_session(self, title: str | None = None) -> ChatSession:
        if title is not None and not isinstance(title, str):
            raise TypeError("title must be a string or None")
        normalized_title = title.strip() or None if title is not None else None
        now = _utc_now()
        session = ChatSession(
            id=str(uuid4()),
            title=normalized_title,
            created_at=now,
            updated_at=now,
        )
        with self._lock, transaction(self._connection_or_raise(), immediate=True):
            self._connection.execute(
                """
                INSERT INTO chat_sessions
                    (id, title, created_at, updated_at, status, archived_at)
                VALUES (?, ?, ?, ?, 'active', NULL)
                """,
                (
                    session.id,
                    session.title,
                    _datetime_text(session.created_at),
                    _datetime_text(session.updated_at),
                ),
            )
        return session

    def get_session(self, session_id: str) -> ChatSession | None:
        with self._lock:
            row = self._connection_or_raise().execute(
                "SELECT * FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return _session_from_row(row) if row is not None else None

    def list_sessions(
        self,
        status: SessionStatus = "active",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[ChatSession, ...]:
        _validate_session_listing(status, limit, offset)
        with self._lock:
            rows = self._connection_or_raise().execute(
                """
                SELECT * FROM chat_sessions
                WHERE status = ?
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (status, limit, offset),
            ).fetchall()
            return tuple(_session_from_row(row) for row in rows)

    def rename_session(self, session_id: str, title: str) -> ChatSession:
        normalized_title = _normalize_session_title(title)
        with self._lock, transaction(self._connection_or_raise(), immediate=True):
            row = self._connection.execute(
                "SELECT * FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown session: {session_id}")
            updated_at = _utc_now()
            self._connection.execute(
                "UPDATE chat_sessions SET title = ?, updated_at = ? WHERE id = ?",
                (normalized_title, _datetime_text(updated_at), session_id),
            )
            return _session_from_row(
                self._connection.execute(
                    "SELECT * FROM chat_sessions WHERE id = ?", (session_id,)
                ).fetchone()
            )

    def archive_session(self, session_id: str) -> bool:
        with self._lock, transaction(self._connection_or_raise(), immediate=True):
            row = self._connection.execute(
                "SELECT status FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None or row["status"] != "active":
                return False
            archived_at = _utc_now()
            self._connection.execute(
                """
                UPDATE chat_sessions
                SET status = 'archived', archived_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (_datetime_text(archived_at), _datetime_text(archived_at), session_id),
            )
            return True

    def restore_session(self, session_id: str) -> bool:
        with self._lock, transaction(self._connection_or_raise(), immediate=True):
            row = self._connection.execute(
                "SELECT status FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None or row["status"] != "archived":
                return False
            restored_at = _utc_now()
            self._connection.execute(
                """
                UPDATE chat_sessions
                SET status = 'active', archived_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (_datetime_text(restored_at), session_id),
            )
            return True

    def delete_session(self, session_id: str) -> bool:
        with self._lock, transaction(self._connection_or_raise(), immediate=True):
            cursor = self._connection.execute(
                "DELETE FROM chat_sessions WHERE id = ?", (session_id,)
            )
            return cursor.rowcount == 1

    def append_message(
        self,
        session_id: str,
        role: MessageRole,
        content: str,
        metadata: Mapping[str, object] | None = None,
    ) -> ChatMessage:
        if not isinstance(role, str) or role not in {
            "system",
            "user",
            "assistant",
            "tool",
        }:
            raise ValueError("role must be system, user, assistant, or tool")
        if not isinstance(content, str):
            raise TypeError("content must be a string")
        metadata_json = _encode_metadata(metadata)
        created_at = _utc_now()
        with self._lock, transaction(self._connection_or_raise(), immediate=True):
            session = self._connection.execute(
                "SELECT id, status, title FROM chat_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise KeyError(f"unknown session: {session_id}")
            if session["status"] == "archived":
                raise SessionArchivedError(f"session is archived: {session_id}")
            cursor = self._connection.execute(
                """
                INSERT INTO chat_messages
                    (session_id, role, content, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    role,
                    content,
                    metadata_json,
                    _datetime_text(created_at),
                ),
            )
            title = session["title"]
            if role == "user" and title is None:
                title = _title_from_user_message(content)
            self._connection.execute(
                "UPDATE chat_sessions SET title = ?, updated_at = ? WHERE id = ?",
                (title, _datetime_text(created_at), session_id),
            )
            message_id = int(cursor.lastrowid)
        return ChatMessage(
            id=message_id,
            session_id=session_id,
            role=role,
            content=content,
            metadata=_decode_metadata(metadata_json),
            created_at=created_at,
        )

    def list_messages(self, session_id: str) -> tuple[ChatMessage, ...]:
        with self._lock:
            connection = self._connection_or_raise()
            if connection.execute(
                "SELECT 1 FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone() is None:
                raise KeyError(f"unknown session: {session_id}")
            rows = connection.execute(
                """
                SELECT id, session_id, role, content, metadata_json, created_at
                FROM chat_messages
                WHERE session_id = ?
                ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()
            return tuple(_message_from_row(row) for row in rows)

    def reserve_report(self, report_date: date, title: str) -> ReportReservation:
        _require_date(report_date, "report_date")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must not be empty")
        normalized_title = title.strip()
        created_at = _utc_now()
        with self._lock, transaction(self._connection_or_raise(), immediate=True):
            cursor = self._connection.execute(
                """
                INSERT INTO reports
                    (report_date, title, file_path, summary, content_hash,
                     created_at, updated_at)
                VALUES (?, ?, ?, '', ?, ?, ?)
                """,
                (
                    report_date.isoformat(),
                    normalized_title,
                    _PLACEHOLDER_PREFIX + str(uuid4()),
                    _PLACEHOLDER_PREFIX + "hash/" + str(uuid4()),
                    _datetime_text(created_at),
                    _datetime_text(created_at),
                ),
            )
            report_id = int(cursor.lastrowid)
            self._connection.execute(
                """
                UPDATE reports
                SET file_path = ?, content_hash = ?
                WHERE id = ?
                """,
                (
                    _pending_placeholder_path(report_id),
                    _pending_placeholder_hash(report_id),
                    report_id,
                ),
            )
        return ReportReservation(
            id=report_id,
            report_date=report_date,
            title=normalized_title,
            created_at=created_at,
        )

    def finalize_report(
        self,
        report_id: int,
        file_path: str,
        summary: str,
        content_hash: str,
    ) -> ReportRecord:
        _require_positive_id(report_id, "report_id")
        _validate_report_fields(file_path, summary, content_hash)
        with self._lock, transaction(self._connection_or_raise(), immediate=True):
            row = self._connection.execute(
                "SELECT * FROM reports WHERE id = ?",
                (report_id,),
            ).fetchone()
            if row is None or not _is_pending_placeholder(row["file_path"]):
                raise KeyError(f"unknown report reservation: {report_id}")
            updated_at = _utc_now()
            self._connection.execute(
                """
                UPDATE reports
                SET file_path = ?, summary = ?, content_hash = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    file_path,
                    summary,
                    content_hash,
                    _datetime_text(updated_at),
                    report_id,
                ),
            )
            result = self._connection.execute(
                "SELECT * FROM reports WHERE id = ?", (report_id,)
            ).fetchone()
        assert result is not None
        return _report_from_row(result)

    def cancel_report(self, report_id: int) -> bool:
        _require_positive_id(report_id, "report_id")
        with self._lock, transaction(self._connection_or_raise(), immediate=True):
            row = self._connection.execute(
                "SELECT file_path FROM reports WHERE id = ?", (report_id,)
            ).fetchone()
            if row is None or not _is_pending_placeholder(row["file_path"]):
                return False
            self._connection.execute(
                "DELETE FROM report_sources WHERE report_id = ?", (report_id,)
            )
            self._connection.execute(
                """
                UPDATE reports
                SET file_path = ?, summary = '', content_hash = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    _cancelled_placeholder_path(report_id),
                    _cancelled_placeholder_hash(report_id),
                    _utc_now_text(),
                    report_id,
                ),
            )
            return True

    def delete_report(self, report_id: int) -> bool:
        _require_positive_id(report_id, "report_id")
        with self._lock, transaction(self._connection_or_raise(), immediate=True):
            row = self._connection.execute(
                """
                SELECT 1 FROM reports
                WHERE id = ? AND file_path NOT GLOB ?
                """,
                (report_id, _PLACEHOLDER_PREFIX + "*"),
            ).fetchone()
            if row is None:
                return False
            self._connection.execute("DELETE FROM reports WHERE id = ?", (report_id,))
            return True

    def list_reports(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[ReportRecord, ...]:
        _validate_report_listing(start_date, end_date, limit, offset)
        clauses = ["file_path NOT GLOB ?"]
        parameters: list[object] = [_PLACEHOLDER_PREFIX + "*"]
        if start_date is not None:
            clauses.append("report_date >= ?")
            parameters.append(start_date.isoformat())
        if end_date is not None:
            clauses.append("report_date <= ?")
            parameters.append(end_date.isoformat())
        parameters.extend((limit, offset))
        with self._lock:
            rows = self._connection_or_raise().execute(
                f"""
                SELECT * FROM reports
                WHERE {' AND '.join(clauses)}
                ORDER BY report_date DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
            return tuple(_report_from_row(row) for row in rows)

    def get_report(self, report_id: int) -> ReportRecord | None:
        _require_positive_id(report_id, "report_id")
        with self._lock:
            row = self._connection_or_raise().execute(
                """
                SELECT * FROM reports
                WHERE id = ? AND file_path NOT GLOB ?
                """,
                (report_id, _PLACEHOLDER_PREFIX + "*"),
            ).fetchone()
            return _report_from_row(row) if row is not None else None

    def add_report_source(
        self,
        report_id: int,
        title: str,
        url: str,
        source_name: str,
        published_at: str | None = None,
        snippet: str = "",
        archive_file_path: str | None = None,
    ) -> ReportSource:
        _require_positive_id(report_id, "report_id")
        if not all(isinstance(value, str) for value in (title, url, source_name, snippet)):
            raise TypeError("source title, URL, name, and snippet must be strings")
        if published_at is not None and not isinstance(published_at, str):
            raise TypeError("published_at must be a string or None")
        if archive_file_path is not None and not isinstance(archive_file_path, str):
            raise TypeError("archive_file_path must be a string or None")
        with self._lock, transaction(self._connection_or_raise(), immediate=True):
            report = self._connection.execute(
                "SELECT file_path FROM reports WHERE id = ?", (report_id,)
            ).fetchone()
            if report is None or _is_cancelled_placeholder(report["file_path"]):
                raise KeyError(f"unknown report: {report_id}")
            existing = self._connection.execute(
                """
                SELECT * FROM report_sources
                WHERE report_id = ? AND url = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (report_id, url),
            ).fetchone()
            if existing is not None:
                return _source_from_row(existing)
            cursor = self._connection.execute(
                """
                INSERT INTO report_sources
                    (report_id, title, url, source_name, published_at, snippet,
                     archive_file_path)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    title,
                    url,
                    source_name,
                    published_at,
                    snippet,
                    archive_file_path,
                ),
            )
            source_id = int(cursor.lastrowid)
        return ReportSource(
            id=source_id,
            report_id=report_id,
            title=title,
            url=url,
            source_name=source_name,
            published_at=published_at,
            snippet=snippet,
            archive_file_path=archive_file_path,
        )

    def get_report_sources(self, report_id: int) -> tuple[ReportSource, ...]:
        _require_positive_id(report_id, "report_id")
        with self._lock:
            connection = self._connection_or_raise()
            if connection.execute(
                """
                SELECT 1 FROM reports
                WHERE id = ? AND file_path NOT GLOB ?
                """,
                (report_id, _PLACEHOLDER_PREFIX + "*"),
            ).fetchone() is None:
                return ()
            rows = connection.execute(
                """
                SELECT * FROM report_sources
                WHERE report_id = ?
                ORDER BY id ASC
                """,
                (report_id,),
            ).fetchall()
            return tuple(_source_from_row(row) for row in rows)

    def get_source(self, source_id: int) -> ReportSource | None:
        _require_positive_id(source_id, "source_id")
        with self._lock:
            row = self._connection_or_raise().execute(
                """
                SELECT source.*
                FROM report_sources AS source
                JOIN reports AS report ON report.id = source.report_id
                WHERE source.id = ? AND report.file_path NOT GLOB ?
                """,
                (source_id, _PLACEHOLDER_PREFIX + "*"),
            ).fetchone()
            return _source_from_row(row) if row is not None else None

    def _connection_or_raise(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("repository is closed")
        return self._connection


def _normalize_preferences(value: UserPreferences) -> UserPreferences:
    if not isinstance(value, UserPreferences):
        raise TypeError("preferences must be UserPreferences")
    return UserPreferences(
        display_name=value.display_name,
        email=value.email,
        timezone=value.timezone,
        topics=value.topics,
        keywords=value.keywords,
    )


def _preferences_from_row(row: sqlite3.Row) -> UserPreferences:
    topics = _load_json_array(row["topics_json"], "topics_json")
    keywords = _load_json_array(row["keywords_json"], "keywords_json")
    return UserPreferences(
        display_name=row["display_name"] or "",
        email=row["email"] or "",
        timezone=row["timezone"],
        topics=topics,
        keywords=keywords,
    )


def _session_from_row(row: sqlite3.Row) -> ChatSession:
    return ChatSession(
        id=row["id"],
        title=row["title"],
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
        status=row["status"],
        archived_at=(
            _parse_datetime(row["archived_at"])
            if row["archived_at"] is not None
            else None
        ),
    )


def _message_from_row(row: sqlite3.Row) -> ChatMessage:
    return ChatMessage(
        id=int(row["id"]),
        session_id=row["session_id"],
        role=row["role"],
        content=row["content"],
        metadata=_decode_metadata(row["metadata_json"]),
        created_at=_parse_datetime(row["created_at"]),
    )


def _report_from_row(row: sqlite3.Row) -> ReportRecord:
    return ReportRecord(
        id=int(row["id"]),
        report_date=date.fromisoformat(row["report_date"]),
        title=row["title"],
        file_path=row["file_path"],
        summary=row["summary"],
        content_hash=row["content_hash"],
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _source_from_row(row: sqlite3.Row) -> ReportSource:
    return ReportSource(
        id=int(row["id"]),
        report_id=int(row["report_id"]),
        title=row["title"],
        url=row["url"],
        source_name=row["source_name"],
        published_at=row["published_at"],
        snippet=row["snippet"],
        archive_file_path=row["archive_file_path"],
    )


def _encode_metadata(value: Mapping[str, object] | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("metadata must be a mapping or None")
    detached = _copy_metadata(value, ())
    try:
        return json.dumps(
            detached,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise ValueError("metadata must contain JSON-compatible values") from None


def _copy_metadata(value: Any, path: tuple[str, ...]) -> Any:
    if isinstance(value, Mapping):
        result = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("metadata keys must be strings")
            normalized_key = key.strip().lower().replace(" ", "_")
            if any(part in normalized_key for part in _FORBIDDEN_METADATA_PARTS):
                location = ".".join((*path, key))
                raise ValueError(f"sensitive metadata field is not allowed: {location}")
            result[key] = _copy_metadata(child, (*path, key))
        return result
    if isinstance(value, (list, tuple)):
        return [_copy_metadata(child, path) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("metadata must contain JSON-compatible values")


def _decode_metadata(value: str | None) -> Mapping[str, object] | None:
    if value is None:
        return None
    try:
        decoded = json.loads(value, parse_constant=_reject_json_constant)
    except (TypeError, ValueError):
        raise ValueError("stored message metadata is invalid JSON") from None
    if not isinstance(decoded, Mapping):
        raise ValueError("stored message metadata must be a JSON object")
    return _freeze_json(_copy_metadata(decoded, ()))


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _dump_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise ValueError("value must contain JSON-compatible values") from None


def _load_json_array(value: str, field_name: str) -> list[str]:
    try:
        decoded = json.loads(value, parse_constant=_reject_json_constant)
    except (TypeError, ValueError):
        raise ValueError(f"stored {field_name} is invalid JSON") from None
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        raise ValueError(f"stored {field_name} must be a JSON string array")
    return decoded


def _validate_session_listing(
    status: SessionStatus,
    limit: int,
    offset: int,
) -> None:
    if status not in {"active", "archived"}:
        raise ValueError("status must be active or archived")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")


def _normalize_session_title(title: str) -> str:
    if not isinstance(title, str):
        raise TypeError("title must be a string")
    normalized = title.strip()
    if not normalized:
        raise ValueError("title must not be empty")
    return normalized


def _title_from_user_message(content: str) -> str | None:
    normalized = " ".join(content.split())
    return normalized[:80] or None


def _pending_placeholder_path(report_id: int) -> str:
    return f"{_PENDING_PLACEHOLDER_PREFIX}{report_id}"


def _pending_placeholder_hash(report_id: int) -> str:
    return f"{_PENDING_PLACEHOLDER_PREFIX}{report_id}.hash"


def _cancelled_placeholder_path(report_id: int) -> str:
    return f"{_CANCELLED_PLACEHOLDER_PREFIX}{report_id}"


def _cancelled_placeholder_hash(report_id: int) -> str:
    return f"{_CANCELLED_PLACEHOLDER_PREFIX}{report_id}.hash"


def _is_placeholder(file_path: str) -> bool:
    return isinstance(file_path, str) and file_path.startswith(_PLACEHOLDER_PREFIX)


def _is_pending_placeholder(file_path: str) -> bool:
    return isinstance(file_path, str) and file_path.startswith(
        _PENDING_PLACEHOLDER_PREFIX
    )


def _is_cancelled_placeholder(file_path: str) -> bool:
    return isinstance(file_path, str) and file_path.startswith(
        _CANCELLED_PLACEHOLDER_PREFIX
    )


def _validate_report_fields(file_path: str, summary: str, content_hash: str) -> None:
    if not isinstance(file_path, str) or not file_path.strip():
        raise ValueError("file_path must not be empty")
    if _is_placeholder(file_path):
        raise ValueError("file_path uses a reserved repository path")
    if not isinstance(summary, str):
        raise TypeError("summary must be a string")
    if not isinstance(content_hash, str) or not content_hash:
        raise ValueError("content_hash must not be empty")


def _validate_report_listing(
    start_date: date | None,
    end_date: date | None,
    limit: int,
    offset: int,
) -> None:
    if start_date is not None:
        _require_date(start_date, "start_date")
    if end_date is not None:
        _require_date(end_date, "end_date")
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")


def _require_positive_id(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")


def _require_date(value: date, field_name: str) -> None:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a date")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _datetime_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _utc_now_text() -> str:
    return _datetime_text(_utc_now())


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant: {value}")
