"""Application services."""

from .digest import (
    DigestConfigurationError,
    DigestError,
    DigestEmptyResponseError,
    DigestIncompleteRunError,
    DigestNoSourcesError,
    DigestPreferences,
    DigestResult,
    DigestService,
    DigestStorageError,
)
from .chat import (
    ChatAgentError,
    ChatResult,
    ChatService,
    ChatSessionArchivedError,
    ChatSessionNotFoundError,
)
from .report import ReportDeletionError, ReportService
from .bash import BashAudit, BashService, ChatRunBusyError, ChatRunManager
from .email import (
    EmailSendResult,
    EmailService,
    EmailServiceError,
    ReportContentUnavailableError,
    ReportNotFoundError,
    build_report_email_body,
    classify_smtp_error,
)
from .scheduler import DailyDigestScheduler, Scheduler

# 汇总各 service 的公开接口，方便调用方统一从包根导入。
__all__ = [
    "DigestConfigurationError",
    "DigestError",
    "DigestEmptyResponseError",
    "DigestIncompleteRunError",
    "DigestNoSourcesError",
    "DigestPreferences",
    "DigestResult",
    "DigestService",
    "DigestStorageError",
    "ChatAgentError",
    "ChatResult",
    "ChatService",
    "ChatSessionArchivedError",
    "ChatSessionNotFoundError",
    "ReportDeletionError",
    "ReportService",
    "BashAudit",
    "BashService",
    "ChatRunBusyError",
    "ChatRunManager",
    "EmailSendResult",
    "EmailService",
    "EmailServiceError",
    "ReportContentUnavailableError",
    "ReportNotFoundError",
    "build_report_email_body",
    "classify_smtp_error",
    "DailyDigestScheduler",
    "Scheduler",
]
