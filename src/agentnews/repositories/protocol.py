from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MessageRole = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class UserPreferences:
    display_name: str = ""
    email: str = ""
    timezone: str = "Asia/Shanghai"
    topics: tuple[str, ...] | Sequence[str] = ()
    keywords: tuple[str, ...] | Sequence[str] = ()

    def __post_init__(self) -> None:
        for name in ("display_name", "email", "timezone"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} must be a string")
        timezone = self.timezone.strip()
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("timezone must be a valid IANA timezone") from None
        object.__setattr__(self, "display_name", self.display_name.strip())
        object.__setattr__(self, "email", self.email.strip())
        object.__setattr__(self, "timezone", timezone)
        object.__setattr__(self, "topics", _normalized_values(self.topics, "topics"))
        object.__setattr__(self, "keywords", _normalized_values(self.keywords, "keywords"))


def _normalized_values(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings")
    result = []
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"{field_name} must contain only strings")
        normalized = value.strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class ChatSession:
    id: str
    title: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ChatMessage:
    id: int
    session_id: str
    role: MessageRole
    content: str
    metadata: Mapping[str, object] | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReportReservation:
    id: int
    report_date: date
    title: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReportRecord:
    id: int
    report_date: date
    title: str
    file_path: str
    summary: str
    content_hash: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ReportSource:
    id: int
    report_id: int
    title: str
    url: str
    source_name: str
    published_at: str | None
    snippet: str
    archive_file_path: str | None


class RepositoryProtocol(Protocol):
    """Synchronous application repository contract, independent of storage backend."""

    def get_preferences(self) -> UserPreferences: ...

    def save_preferences(self, preferences: UserPreferences) -> UserPreferences: ...

    def create_session(self, title: str | None = None) -> ChatSession: ...

    def append_message(
        self,
        session_id: str,
        role: MessageRole,
        content: str,
        metadata: Mapping[str, object] | None = None,
    ) -> ChatMessage: ...

    def list_messages(self, session_id: str) -> tuple[ChatMessage, ...]: ...

    def reserve_report(self, report_date: date, title: str) -> ReportReservation: ...

    def finalize_report(
        self,
        report_id: int,
        file_path: str,
        summary: str,
        content_hash: str,
    ) -> ReportRecord: ...

    def cancel_report(self, report_id: int) -> bool: ...

    def list_reports(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[ReportRecord, ...]: ...

    def get_report(self, report_id: int) -> ReportRecord | None: ...

    def add_report_source(
        self,
        report_id: int,
        title: str,
        url: str,
        source_name: str,
        published_at: str | None = None,
        snippet: str = "",
        archive_file_path: str | None = None,
    ) -> ReportSource: ...

    def get_report_sources(self, report_id: int) -> tuple[ReportSource, ...]: ...

    def get_source(self, source_id: int) -> ReportSource | None: ...
