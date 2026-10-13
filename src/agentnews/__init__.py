from .config import Settings, get_settings
from .db import initialize_database
from .report_storage import (
    InvalidReportPathError,
    ReportConflictError,
    ReportStorage,
    StoredReport,
)

__all__ = [
    "InvalidReportPathError",
    "ReportConflictError",
    "ReportStorage",
    "Settings",
    "StoredReport",
    "get_settings",
    "initialize_database",
    "main",
]


def main(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    initialize_database(settings.sqlite_path)
    print("AgentNews command-line scaffold is ready.")
