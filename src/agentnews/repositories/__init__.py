"""Persistence adapters for reports and other application data."""

from .memory import InMemoryRepository
from .protocol import (
    ChatMessage,
    ChatSession,
    MessageRole,
    ReportRecord,
    ReportReservation,
    ReportSource,
    RepositoryProtocol,
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
    "InvalidReportPathError",
    "ReportConflictError",
    "ReportRecord",
    "ReportReservation",
    "ReportSource",
    "ReportStorage",
    "RepositoryProtocol",
    "StoredReport",
    "InvalidSourceArchivePathError",
    "SourceArchiveStorage",
    "StoredSourceArchive",
    "UserPreferences",
    "MessageRole",
]
