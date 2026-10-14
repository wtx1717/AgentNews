from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
import json
import threading
import time
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from .protocol import (
    ChatMessage,
    ChatSession,
    MessageRole,
    ReportRecord,
    ReportReservation,
    ReportSource,
    UserPreferences,
)


_REPORT_ID_LOCK = threading.Lock()
_NEXT_PROCESS_REPORT_ID = 0


class InMemoryRepository:
    """Thread-safe, process-local implementation of the application repository."""

    def __init__(self, *, report_id_factory: Callable[[], int] | None = None) -> None:
        self._lock = threading.RLock()
        self._report_id_factory = report_id_factory
        self._preferences = UserPreferences()
        self._sessions: dict[str, ChatSession] = {}
        self._messages: dict[str, list[ChatMessage]] = {}
        self._reservations: dict[int, ReportReservation] = {}
        self._reports: dict[int, ReportRecord] = {}
        self._report_sources: dict[int, list[ReportSource]] = {}
        self._next_message_id = 1
        self._next_test_report_id: int | None = None
        self._next_source_id = 1

    def get_preferences(self) -> UserPreferences:
        with self._lock:
            return self._preferences

    def save_preferences(self, preferences: UserPreferences) -> UserPreferences:
        normalized = _normalize_preferences(preferences)
        with self._lock:
            self._preferences = normalized
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
        with self._lock:
            self._sessions[session.id] = session
            self._messages[session.id] = []
        return session

    def append_message(
        self,
        session_id: str,
        role: MessageRole,
        content: str,
        metadata: Mapping[str, object] | None = None,
    ) -> ChatMessage:
        if not isinstance(role, str) or role not in {"system", "user", "assistant", "tool"}:
            raise ValueError("role must be system, user, assistant, or tool")
        if not isinstance(content, str):
            raise TypeError("content must be a string")
        frozen_metadata = _freeze_metadata(metadata)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(f"unknown session: {session_id}")
            message = ChatMessage(
                id=self._next_message_id,
                session_id=session_id,
                role=role,
                content=content,
                metadata=frozen_metadata,
                created_at=_utc_now(),
            )
            self._next_message_id += 1
            self._messages[session_id].append(message)
            self._sessions[session_id] = ChatSession(
                id=session.id,
                title=session.title,
                created_at=session.created_at,
                updated_at=message.created_at,
            )
            return message

    def list_messages(self, session_id: str) -> tuple[ChatMessage, ...]:
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError(f"unknown session: {session_id}")
            return tuple(self._messages[session_id])

    def reserve_report(self, report_date: date, title: str) -> ReportReservation:
        _require_date(report_date, "report_date")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must not be empty")
        with self._lock:
            report_id = self._allocate_report_id()
            reservation = ReportReservation(
                id=report_id,
                report_date=report_date,
                title=title.strip(),
                created_at=_utc_now(),
            )
            self._reservations[reservation.id] = reservation
            self._report_sources[reservation.id] = []
            return reservation

    def _allocate_report_id(self) -> int:
        if self._report_id_factory is None:
            return _allocate_process_report_id()
        if self._next_test_report_id is None:
            value = self._report_id_factory()
            _require_positive_id(value, "report_id")
            self._next_test_report_id = value
        report_id = self._next_test_report_id
        self._next_test_report_id += 1
        return report_id

    def finalize_report(
        self,
        report_id: int,
        file_path: str,
        summary: str,
        content_hash: str,
    ) -> ReportRecord:
        _require_positive_id(report_id, "report_id")
        if not isinstance(file_path, str) or not file_path.strip():
            raise ValueError("file_path must not be empty")
        if not isinstance(summary, str):
            raise TypeError("summary must be a string")
        if not isinstance(content_hash, str) or not content_hash:
            raise ValueError("content_hash must not be empty")
        with self._lock:
            reservation = self._reservations.pop(report_id, None)
            if reservation is None:
                raise KeyError(f"unknown report reservation: {report_id}")
            now = _utc_now()
            report = ReportRecord(
                id=reservation.id,
                report_date=reservation.report_date,
                title=reservation.title,
                file_path=file_path,
                summary=summary,
                content_hash=content_hash,
                created_at=reservation.created_at,
                updated_at=now,
            )
            self._reports[report_id] = report
            return report

    def cancel_report(self, report_id: int) -> bool:
        _require_positive_id(report_id, "report_id")
        with self._lock:
            if self._reservations.pop(report_id, None) is None:
                return False
            self._report_sources.pop(report_id, None)
            return True

    def list_reports(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[ReportRecord, ...]:
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
        with self._lock:
            reports = [
                report
                for report in self._reports.values()
                if (start_date is None or report.report_date >= start_date)
                and (end_date is None or report.report_date <= end_date)
            ]
            reports.sort(key=lambda report: (report.report_date, report.id), reverse=True)
            return tuple(reports[offset : offset + limit])

    def get_report(self, report_id: int) -> ReportRecord | None:
        _require_positive_id(report_id, "report_id")
        with self._lock:
            return self._reports.get(report_id)

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
        values = (title, url, source_name, snippet)
        if not all(isinstance(value, str) for value in values):
            raise TypeError("source title, URL, name, and snippet must be strings")
        if published_at is not None and not isinstance(published_at, str):
            raise TypeError("published_at must be a string or None")
        if archive_file_path is not None and not isinstance(archive_file_path, str):
            raise TypeError("archive_file_path must be a string or None")
        with self._lock:
            if report_id not in self._reservations and report_id not in self._reports:
                raise KeyError(f"unknown report: {report_id}")
            current_sources = self._report_sources.setdefault(report_id, [])
            for existing in current_sources:
                if existing.url == url:
                    return existing
            source = ReportSource(
                id=self._next_source_id,
                report_id=report_id,
                title=title,
                url=url,
                source_name=source_name,
                published_at=published_at,
                snippet=snippet,
                archive_file_path=archive_file_path,
            )
            self._next_source_id += 1
            current_sources.append(source)
            return source

    def get_report_sources(self, report_id: int) -> tuple[ReportSource, ...]:
        _require_positive_id(report_id, "report_id")
        with self._lock:
            if report_id not in self._reports:
                return ()
            return tuple(self._report_sources.get(report_id, ()))


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


def _freeze_metadata(value: Mapping[str, object] | None) -> Mapping[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("metadata must be a mapping or None")
    try:
        detached = json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        raise ValueError("metadata must contain JSON-compatible values") from None
    return _freeze_json(detached)


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _require_positive_id(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")


def _require_date(value: date, field_name: str) -> None:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a date")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _allocate_process_report_id() -> int:
    global _NEXT_PROCESS_REPORT_ID
    with _REPORT_ID_LOCK:
        timestamp_id = max(1, time.time_ns() // 1_000)
        _NEXT_PROCESS_REPORT_ID = max(timestamp_id, _NEXT_PROCESS_REPORT_ID + 1)
        return _NEXT_PROCESS_REPORT_ID
