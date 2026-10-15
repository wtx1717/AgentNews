"""Persistence adapters for reports and other application data."""

from .memory import InMemoryRepository
from .sqlite import SQLiteRepository
from .protocol import (
    ChatMessage,
    ChatSession,
    MessageRole,
    ReportRecord,
    ReportReservation,
    ReportSource,
    RepositoryProtocol,
    SessionArchivedError,
    SessionStatus,
    UserPreferences,
)
from .report_storage import (
    InvalidReportPathError,
    ReportConflictError,
    ReportStorage,
    StoredReport,
)
from .source_archive_storage import (
    InvalidSourceArchivePathError,
    SourceArchiveStorage,
    StoredSourceArchive,
)

__all__ = [
    "ChatMessage",
    "ChatSession",
    "InMemoryRepository",
    "SQLiteRepository",
    "InvalidReportPathError",
    "ReportConflictError",
    "ReportRecord",
    "ReportReservation",
    "ReportSource",
    "ReportStorage",
    "RepositoryProtocol",
    "SessionArchivedError",
    "SessionStatus",
    "StoredReport",
    "InvalidSourceArchivePathError",
    "SourceArchiveStorage",
    "StoredSourceArchive",
    "UserPreferences",
    "MessageRole",
]
