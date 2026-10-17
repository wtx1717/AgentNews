"""仓储层的数据模型与协议定义，不依赖具体存储后端。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MessageRole = Literal["system", "user", "assistant", "tool"]
SessionStatus = Literal["active", "archived"]
EmailDeliveryStatus = Literal["pending", "sent", "failed"]
EmailDeliverySource = Literal["manual_api", "natural_language", "scheduled_task"]
ScheduledDigestRunStatus = Literal["pending", "running", "succeeded", "failed", "skipped"]


class SessionArchivedError(RuntimeError):
    """Raised when a write is attempted against an archived session."""


@dataclass(frozen=True, slots=True)
class UserPreferences:
    """用户偏好；构造时校验时区并规范化主题、关键词与发送时间。"""
    display_name: str = ""
    email: str = ""
    timezone: str = "Asia/Shanghai"
    topics: tuple[str, ...] | Sequence[str] = ()
    keywords: tuple[str, ...] | Sequence[str] = ()
    daily_send_enabled: bool = False
    daily_send_time: str = "08:00"

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
        if not isinstance(self.daily_send_enabled, bool):
            raise TypeError("daily_send_enabled must be a boolean")
        object.__setattr__(self, "daily_send_time", normalize_daily_send_time(self.daily_send_time))


def _normalized_values(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    """去空白、去重并保持首次出现的顺序。"""
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


def normalize_daily_send_time(value: str) -> str:
    """Validate and normalize a daily schedule time to strict ``HH:MM``."""
    if not isinstance(value, str):
        raise TypeError("daily_send_time must be a string")
    if len(value) != 5 or value[2] != ":" or not value[:2].isdigit() or not value[3:].isdigit():
        raise ValueError("daily_send_time must use HH:MM format")
    hour, minute = int(value[:2]), int(value[3:])
    if hour > 23 or minute > 59:
        raise ValueError("daily_send_time must use HH:MM format")
    return f"{hour:02d}:{minute:02d}"


@dataclass(frozen=True, slots=True)
class ChatSession:
    """会话元数据，状态只允许 active 与 archived。"""
    id: str
    title: str | None
    created_at: datetime
    updated_at: datetime
    status: SessionStatus = "active"
    archived_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """单条会话消息，metadata 只承载安全的 JSON 值。"""
    id: int
    session_id: str
    role: MessageRole
    content: str
    metadata: Mapping[str, object] | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReportReservation:
    """报告预占记录，表示记录已占位但文件尚未生成。"""
    id: int
    report_date: date
    title: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReportRecord:
    """已落定的报告记录，含文件路径、摘要与内容哈希。"""
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
    """报告引用的单条来源及其可选归档文件。"""
    id: int
    report_id: int
    title: str
    url: str
    source_name: str
    published_at: str | None
    snippet: str
    archive_file_path: str | None


@dataclass(frozen=True, slots=True)
class EmailDeliveryRecord:
    """一次邮件投递的当前状态与重试信息。"""
    id: int
    report_id: int
    recipient: str
    subject: str
    status: EmailDeliveryStatus
    error_code: str | None
    error_message: str | None
    retry_count: int
    trigger_source: EmailDeliverySource
    idempotency_key: str | None
    requested_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class EmailDeliveryAttempt:
    """单次邮件投递尝试的结果。"""
    id: int
    delivery_id: int
    attempt_number: int
    status: Literal["sent", "failed"]
    error_code: str | None
    error_message: str | None
    attempted_at: datetime
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class ScheduledDigestRun:
    """按业务日期记录的定时摘要任务执行状态。"""
    id: int
    business_date: date
    status: ScheduledDigestRunStatus
    attempt_count: int
    report_id: int | None
    email_delivery_id: int | None
    started_at: datetime | None
    completed_at: datetime | None
    error_code: str | None


class RepositoryProtocol(Protocol):
    """Synchronous application repository contract, independent of storage backend."""

    def get_preferences(self) -> UserPreferences: ...

    def save_preferences(self, preferences: UserPreferences) -> UserPreferences: ...

    # 同日重复创建时返回既有记录，第二个返回值表示是否新建。
    def create_scheduled_run(
        self,
        business_date: date,
        *,
        status: ScheduledDigestRunStatus = "pending",
        error_code: str | None = None,
    ) -> tuple[ScheduledDigestRun, bool]: ...

    # 抢占待执行任务；已被抢占或重试超限时返回 None。
    def claim_scheduled_run(self, run_id: int) -> ScheduledDigestRun | None: ...

    def get_scheduled_run(self, business_date: date) -> ScheduledDigestRun | None: ...

    def list_scheduled_runs(self, limit: int = 20) -> tuple[ScheduledDigestRun, ...]: ...

    # 只更新显式传入的字段，None 表示保留原值。
    def update_scheduled_run(
        self,
        run_id: int,
        status: ScheduledDigestRunStatus,
        *,
        attempt_count: int | None = None,
        report_id: int | None = None,
        email_delivery_id: int | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        error_code: str | None = None,
    ) -> ScheduledDigestRun: ...

    def create_session(self, title: str | None = None) -> ChatSession: ...

    def get_session(self, session_id: str) -> ChatSession | None: ...

    def list_sessions(
        self,
        status: SessionStatus = "active",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[ChatSession, ...]: ...

    def rename_session(self, session_id: str, title: str) -> ChatSession: ...

    # 仅活跃会话可归档，返回是否发生状态迁移。
    def archive_session(self, session_id: str) -> bool: ...

    # 仅已归档会话可恢复，返回是否发生状态迁移。
    def restore_session(self, session_id: str) -> bool: ...

    def delete_session(self, session_id: str) -> bool: ...

    # 首条用户消息会顺带生成会话标题。
    def append_message(
        self,
        session_id: str,
        role: MessageRole,
        content: str,
        metadata: Mapping[str, object] | None = None,
    ) -> ChatMessage: ...

    def list_messages(self, session_id: str) -> tuple[ChatMessage, ...]: ...

    # 先占位，文件生成成功后再调用 finalize_report 落定。
    def reserve_report(self, report_date: date, title: str) -> ReportReservation: ...

    # 仅待落定的占位报告允许落定，重复落定会抛错。
    def finalize_report(
        self,
        report_id: int,
        file_path: str,
        summary: str,
        content_hash: str,
    ) -> ReportRecord: ...

    # 取消预占并清空来源，记录保留为 cancelled 占位。
    def cancel_report(self, report_id: int) -> bool: ...

    # 只允许删除已落定的报告，占位记录不可删。
    def delete_report(self, report_id: int) -> bool: ...

    def list_reports(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[ReportRecord, ...]: ...

    def get_report(self, report_id: int) -> ReportRecord | None: ...

    # 同一报告内按 URL 去重，重复添加返回已有记录。
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

    # 占位报告返回空元组，不暴露内部状态。
    def get_report_sources(self, report_id: int) -> tuple[ReportSource, ...]: ...

    def get_source(self, source_id: int) -> ReportSource | None: ...

    # 按幂等键去重，第二个返回值表示是否新建。
    def reserve_email_delivery(
        self,
        report_id: int,
        recipient: str,
        subject: str,
        trigger_source: EmailDeliverySource,
        idempotency_key: str,
    ) -> tuple[EmailDeliveryRecord, bool]: ...

    def get_email_delivery(self, delivery_id: int) -> EmailDeliveryRecord | None: ...

    def list_email_deliveries(self, report_id: int) -> tuple[EmailDeliveryRecord, ...]: ...

    def list_email_delivery_attempts(
        self, delivery_id: int
    ) -> tuple[EmailDeliveryAttempt, ...]: ...

    def record_email_delivery_attempt(
        self,
        delivery_id: int,
        attempt_number: int,
        status: Literal["sent", "failed"],
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> EmailDeliveryAttempt: ...

    # 状态为 pending 时清空完成时间，其余状态视为终态。
    def update_email_delivery(
        self,
        delivery_id: int,
        status: EmailDeliveryStatus,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        retry_count: int | None = None,
    ) -> EmailDeliveryRecord: ...
