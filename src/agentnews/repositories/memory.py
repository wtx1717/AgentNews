"""进程内存储实现。

用线程安全的内存字典模拟会话、报告、邮件投递等仓储行为，供测试与本地运行使用。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
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
    EmailDeliveryAttempt,
    EmailDeliveryRecord,
    EmailDeliverySource,
    EmailDeliveryStatus,
    MessageRole,
    ReportRecord,
    ReportReservation,
    ReportSource,
    ScheduledDigestRun,
    ScheduledDigestRunStatus,
    SessionArchivedError,
    SessionStatus,
    UserPreferences,
    normalize_daily_send_time,
)


# 进程级报告 ID 计数器需要跨实例加锁。
_REPORT_ID_LOCK = threading.Lock()
_NEXT_PROCESS_REPORT_ID = 0


class InMemoryRepository:
    """Thread-safe, process-local implementation of the application repository."""

    def __init__(self, *, report_id_factory: Callable[[], int] | None = None) -> None:
        # 用可重入锁，允许持锁的公开方法内部再调用其他加锁方法。
        self._lock = threading.RLock()
        self._report_id_factory = report_id_factory
        self._preferences = UserPreferences()
        self._sessions: dict[str, ChatSession] = {}
        self._messages: dict[str, list[ChatMessage]] = {}
        self._reservations: dict[int, ReportReservation] = {}
        self._reports: dict[int, ReportRecord] = {}
        self._report_sources: dict[int, list[ReportSource]] = {}
        self._email_deliveries: dict[int, EmailDeliveryRecord] = {}
        self._email_delivery_attempts: dict[int, list[EmailDeliveryAttempt]] = {}
        self._scheduled_runs: dict[date, ScheduledDigestRun] = {}
        self._next_scheduled_run_id = 1
        self._next_message_id = 1
        self._next_test_report_id: int | None = None
        self._next_source_id = 1
        self._next_email_delivery_id = 1
        self._next_email_attempt_id = 1

    def get_preferences(self) -> UserPreferences:
        """返回当前用户偏好。"""
        with self._lock:
            return self._preferences

    def save_preferences(self, preferences: UserPreferences) -> UserPreferences:
        """校验并保存用户偏好，返回规范化后的结果。"""
        normalized = _normalize_preferences(preferences)
        with self._lock:
            self._preferences = normalized
            return normalized

    def create_scheduled_run(
        self,
        business_date: date,
        *,
        status: ScheduledDigestRunStatus = "pending",
        error_code: str | None = None,
    ) -> tuple[ScheduledDigestRun, bool]:
        """为业务日期创建定时摘要记录；同日已存在时直接返回旧记录。"""
        _require_date(business_date, "business_date")
        _validate_scheduled_status(status)
        with self._lock:
            existing = self._scheduled_runs.get(business_date)
            if existing is not None:
                return existing, False
            now = _utc_now()
            run = ScheduledDigestRun(
                id=self._next_scheduled_run_id,
                business_date=business_date,
                status=status,
                attempt_count=0,
                report_id=None,
                email_delivery_id=None,
                started_at=None,
                completed_at=now if status in {"failed", "skipped"} else None,
                error_code=error_code,
            )
            self._next_scheduled_run_id += 1
            self._scheduled_runs[business_date] = run
            return run, True

    def claim_scheduled_run(self, run_id: int) -> ScheduledDigestRun | None:
        """原子地抢占待执行任务并累加尝试次数，已被占用或超限时返回 None。"""
        _require_positive_id(run_id, "run_id")
        with self._lock:
            for business_date, run in self._scheduled_runs.items():
                if run.id == run_id:
                    # 必须同时满足待执行且未超过重试上限，防止任务被重复执行。
                    if run.status != "pending" or run.attempt_count >= 3:
                        return None
                    claimed = replace(
                        run,
                        status="running",
                        attempt_count=run.attempt_count + 1,
                        started_at=_utc_now(),
                        completed_at=None,
                    )
                    self._scheduled_runs[business_date] = claimed
                    return claimed
            return None

    def get_scheduled_run(self, business_date: date) -> ScheduledDigestRun | None:
        """按业务日期查询定时任务。"""
        _require_date(business_date, "business_date")
        with self._lock:
            return self._scheduled_runs.get(business_date)

    def list_scheduled_runs(self, limit: int = 20) -> tuple[ScheduledDigestRun, ...]:
        """按业务日期倒序返回最近的定时任务。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._lock:
            return tuple(
                sorted(
                    self._scheduled_runs.values(),
                    key=lambda item: (item.business_date, item.id),
                    reverse=True,
                )[:limit]
            )

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
    ) -> ScheduledDigestRun:
        """更新定时任务的执行状态及关联的报告、邮件投递等字段。"""
        _require_positive_id(run_id, "run_id")
        _validate_scheduled_status(status)
        if attempt_count is not None and (isinstance(attempt_count, bool) or not isinstance(attempt_count, int) or not 0 <= attempt_count <= 3):
            raise ValueError("attempt_count must be between 0 and 3")
        with self._lock:
            for business_date, run in self._scheduled_runs.items():
                if run.id == run_id:
                    updated = replace(
                        run,
                        status=status,
                        attempt_count=run.attempt_count if attempt_count is None else attempt_count,
                        report_id=run.report_id if report_id is None else report_id,
                        email_delivery_id=run.email_delivery_id if email_delivery_id is None else email_delivery_id,
                        started_at=run.started_at if started_at is None else started_at,
                        completed_at=(
                            _utc_now() if completed_at is None and status in {"succeeded", "failed", "skipped"}
                            else completed_at
                        ),
                        error_code=error_code,
                    )
                    self._scheduled_runs[business_date] = updated
                    return updated
            raise KeyError(f"unknown scheduled run: {run_id}")

    def create_session(self, title: str | None = None) -> ChatSession:
        """创建会话并初始化其消息列表。"""
        if title is not None and not isinstance(title, str):
            raise TypeError("title must be a string or None")
        normalized_title = title.strip() or None if title is not None else None
        now = _utc_now()
        session = ChatSession(
            id=str(uuid4()),
            title=normalized_title,
            created_at=now,
            updated_at=now,
            status="active",
            archived_at=None,
        )
        with self._lock:
            self._sessions[session.id] = session
            self._messages[session.id] = []
        return session

    def get_session(self, session_id: str) -> ChatSession | None:
        """按 ID 查询会话。"""
        with self._lock:
            return self._sessions.get(session_id)

    def list_sessions(
        self,
        status: SessionStatus = "active",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[ChatSession, ...]:
        """按状态分页列出会话，按更新时间倒序。"""
        _validate_session_listing(status, limit, offset)
        with self._lock:
            sessions = [
                session for session in self._sessions.values() if session.status == status
            ]
            sessions.sort(
                key=lambda session: (session.updated_at, session.created_at, session.id),
                reverse=True,
            )
            return tuple(sessions[offset : offset + limit])

    def rename_session(self, session_id: str, title: str) -> ChatSession:
        """修改会话标题并刷新更新时间。"""
        normalized_title = _normalize_session_title(title)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(f"unknown session: {session_id}")
            renamed = ChatSession(
                id=session.id,
                title=normalized_title,
                created_at=session.created_at,
                updated_at=_utc_now(),
                status=session.status,
                archived_at=session.archived_at,
            )
            self._sessions[session_id] = renamed
            return renamed

    def archive_session(self, session_id: str) -> bool:
        """把活跃会话置为归档；不存在或已归档时返回 False。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.status != "active":
                return False
            archived_at = _utc_now()
            self._sessions[session_id] = ChatSession(
                id=session.id,
                title=session.title,
                created_at=session.created_at,
                updated_at=archived_at,
                status="archived",
                archived_at=archived_at,
            )
            return True

    def restore_session(self, session_id: str) -> bool:
        """把归档会话恢复为活跃；不存在或非归档时返回 False。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.status != "archived":
                return False
            restored_at = _utc_now()
            self._sessions[session_id] = ChatSession(
                id=session.id,
                title=session.title,
                created_at=session.created_at,
                updated_at=restored_at,
                status="active",
                archived_at=None,
            )
            return True

    def delete_session(self, session_id: str) -> bool:
        """删除会话及其全部消息；会话不存在时返回 False。"""
        with self._lock:
            if self._sessions.pop(session_id, None) is None:
                return False
            self._messages.pop(session_id, None)
            return True

    def append_message(
        self,
        session_id: str,
        role: MessageRole,
        content: str,
        metadata: Mapping[str, object] | None = None,
    ) -> ChatMessage:
        """向会话追加消息，并同步更新时间与首条用户消息生成的默认标题。"""
        if not isinstance(role, str) or role not in {"system", "user", "assistant", "tool"}:
            raise ValueError("role must be system, user, assistant, or tool")
        if not isinstance(content, str):
            raise TypeError("content must be a string")
        frozen_metadata = _freeze_metadata(metadata)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(f"unknown session: {session_id}")
            if session.status == "archived":
                raise SessionArchivedError(f"session is archived: {session_id}")
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
            title = session.title
            if role == "user" and title is None:
                title = _title_from_user_message(content)
            self._sessions[session_id] = ChatSession(
                id=session.id,
                title=title,
                created_at=session.created_at,
                updated_at=message.created_at,
                status=session.status,
                archived_at=session.archived_at,
            )
            return message

    def list_messages(self, session_id: str) -> tuple[ChatMessage, ...]:
        """按追加顺序返回会话内的全部消息。"""
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError(f"unknown session: {session_id}")
            return tuple(self._messages[session_id])

    def reserve_report(self, report_date: date, title: str) -> ReportReservation:
        """为报告预留 ID 与位置，此时报告内容尚未落盘。"""
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
        """分配报告 ID；注入 ID 工厂时以首次取值为起点递增。"""
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
        """把预留记录转为正式报告记录。"""
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
        """撤销尚未完成的报告预留；预留不存在时返回 False。"""
        _require_positive_id(report_id, "report_id")
        with self._lock:
            if self._reservations.pop(report_id, None) is None:
                return False
            self._report_sources.pop(report_id, None)
            return True

    def delete_report(self, report_id: int) -> bool:
        """删除报告及其来源、关联的邮件投递记录。"""
        _require_positive_id(report_id, "report_id")
        with self._lock:
            if self._reports.pop(report_id, None) is None:
                return False
            self._report_sources.pop(report_id, None)
            delivery_ids = [
                delivery.id
                for delivery in self._email_deliveries.values()
                if delivery.report_id == report_id
            ]
            for delivery_id in delivery_ids:
                self._email_deliveries.pop(delivery_id, None)
                self._email_delivery_attempts.pop(delivery_id, None)
            return True

    def list_reports(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[ReportRecord, ...]:
        """按可选日期区间分页查询报告，按日期与 ID 倒序。"""
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
        """按 ID 查询报告。"""
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
        """为报告添加来源；URL 重复时返回已有记录以保持幂等。"""
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
            # 同一 URL 视为同一来源，重复添加直接复用，避免来源列表膨胀。
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
        """返回报告已记录的来源，报告不存在时返回空元组。"""
        _require_positive_id(report_id, "report_id")
        with self._lock:
            if report_id not in self._reports:
                return ()
            return tuple(self._report_sources.get(report_id, ()))

    def get_source(self, source_id: int) -> ReportSource | None:
        """跨全部报告按来源 ID 查找来源。"""
        _require_positive_id(source_id, "source_id")
        with self._lock:
            for sources in self._report_sources.values():
                for source in sources:
                    if source.id == source_id:
                        return source
            return None

    def reserve_email_delivery(
        self,
        report_id: int,
        recipient: str,
        subject: str,
        trigger_source: EmailDeliverySource,
        idempotency_key: str,
    ) -> tuple[EmailDeliveryRecord, bool]:
        """按幂等键预留邮件投递；键已存在时返回已有记录且第二个元素为 False。"""
        _require_positive_id(report_id, "report_id")
        _validate_delivery_fields(recipient, subject, trigger_source, idempotency_key)
        with self._lock:
            if report_id not in self._reports:
                raise KeyError(f"unknown report: {report_id}")
            for delivery in self._email_deliveries.values():
                # 命中相同幂等键即视为同一请求，直接复用，避免重复发送邮件。
                if delivery.idempotency_key == idempotency_key:
                    return delivery, False
            now = _utc_now()
            delivery = EmailDeliveryRecord(
                id=self._next_email_delivery_id,
                report_id=report_id,
                recipient=recipient,
                subject=subject,
                status="pending",
                error_code=None,
                error_message=None,
                retry_count=0,
                trigger_source=trigger_source,
                idempotency_key=idempotency_key,
                requested_at=now,
                completed_at=None,
            )
            self._next_email_delivery_id += 1
            self._email_deliveries[delivery.id] = delivery
            self._email_delivery_attempts[delivery.id] = []
            return delivery, True

    def get_email_delivery(self, delivery_id: int) -> EmailDeliveryRecord | None:
        """按 ID 查询邮件投递记录。"""
        _require_positive_id(delivery_id, "delivery_id")
        with self._lock:
            return self._email_deliveries.get(delivery_id)

    def list_email_deliveries(
        self, report_id: int
    ) -> tuple[EmailDeliveryRecord, ...]:
        """按请求时间倒序返回指定报告的全部投递记录。"""
        _require_positive_id(report_id, "report_id")
        with self._lock:
            deliveries = [
                delivery
                for delivery in self._email_deliveries.values()
                if delivery.report_id == report_id
            ]
            deliveries.sort(key=lambda item: (item.requested_at, item.id), reverse=True)
            return tuple(deliveries)

    def list_email_delivery_attempts(
        self, delivery_id: int
    ) -> tuple[EmailDeliveryAttempt, ...]:
        """返回某次投递的全部尝试记录。"""
        _require_positive_id(delivery_id, "delivery_id")
        with self._lock:
            return tuple(self._email_delivery_attempts.get(delivery_id, ()))

    def record_email_delivery_attempt(
        self,
        delivery_id: int,
        attempt_number: int,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> EmailDeliveryAttempt:
        """记录一次投递尝试，同一投递内尝试序号必须唯一。"""
        _require_positive_id(delivery_id, "delivery_id")
        _validate_delivery_attempt(attempt_number, status, error_code, error_message)
        with self._lock:
            if delivery_id not in self._email_deliveries:
                raise KeyError(f"unknown email delivery: {delivery_id}")
            attempts = self._email_delivery_attempts[delivery_id]
            # 尝试序号唯一，防止重试被重复计数。
            if any(attempt.attempt_number == attempt_number for attempt in attempts):
                raise ValueError("duplicate email delivery attempt")
            now = _utc_now()
            attempt = EmailDeliveryAttempt(
                id=self._next_email_attempt_id,
                delivery_id=delivery_id,
                attempt_number=attempt_number,
                status=status,
                error_code=error_code,
                error_message=error_message,
                attempted_at=now,
                completed_at=now,
            )
            self._next_email_attempt_id += 1
            attempts.append(attempt)
            return attempt

    def update_email_delivery(
        self,
        delivery_id: int,
        status: EmailDeliveryStatus,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        retry_count: int | None = None,
    ) -> EmailDeliveryRecord:
        """更新投递状态、错误信息与重试次数。"""
        _require_positive_id(delivery_id, "delivery_id")
        if status not in {"pending", "sent", "failed"}:
            raise ValueError("invalid delivery status")
        if retry_count is not None and (
            isinstance(retry_count, bool) or not isinstance(retry_count, int) or retry_count < 0
        ):
            raise ValueError("retry_count must be a non-negative integer")
        with self._lock:
            delivery = self._email_deliveries.get(delivery_id)
            if delivery is None:
                raise KeyError(f"unknown email delivery: {delivery_id}")
            updated = replace(
                delivery,
                status=status,
                error_code=error_code,
                error_message=error_message,
                retry_count=(delivery.retry_count if retry_count is None else retry_count),
                completed_at=None if status == "pending" else _utc_now(),
            )
            self._email_deliveries[delivery_id] = updated
            return updated


def _normalize_preferences(value: UserPreferences) -> UserPreferences:
    if not isinstance(value, UserPreferences):
        raise TypeError("preferences must be UserPreferences")
    return UserPreferences(
        display_name=value.display_name,
        email=value.email,
        timezone=value.timezone,
        topics=value.topics,
        keywords=value.keywords,
        daily_send_enabled=value.daily_send_enabled,
        daily_send_time=normalize_daily_send_time(value.daily_send_time),
    )


def _validate_scheduled_status(status: str) -> None:
    if status not in {"pending", "running", "succeeded", "failed", "skipped"}:
        raise ValueError("invalid scheduled run status")


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


def _freeze_metadata(value: Mapping[str, object] | None) -> Mapping[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("metadata must be a mapping or None")
    try:
        # 先走一次 JSON 往返，把入参与调用方对象彻底解耦，并顺带校验可序列化。
        detached = json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        raise ValueError("metadata must contain JSON-compatible values") from None
    return _freeze_json(detached)


def _freeze_json(value: Any) -> Any:
    """把 JSON 结构递归冻结为只读映射与元组。"""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _require_positive_id(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")


def _validate_delivery_fields(
    recipient: str,
    subject: str,
    trigger_source: str,
    idempotency_key: str,
) -> None:
    if not isinstance(recipient, str) or not isinstance(subject, str):
        raise TypeError("recipient and subject must be strings")
    if trigger_source not in {"manual_api", "natural_language", "scheduled_task"}:
        raise ValueError("invalid email delivery source")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError("idempotency_key must not be empty")


def _validate_delivery_attempt(
    attempt_number: int,
    status: str,
    error_code: str | None,
    error_message: str | None,
) -> None:
    if isinstance(attempt_number, bool) or not isinstance(attempt_number, int) or attempt_number < 1:
        raise ValueError("attempt_number must be a positive integer")
    if status not in {"sent", "failed"}:
        raise ValueError("invalid email attempt status")
    if error_code is not None and not isinstance(error_code, str):
        raise TypeError("error_code must be a string or None")
    if error_message is not None and not isinstance(error_message, str):
        raise TypeError("error_message must be a string or None")


def _require_date(value: date, field_name: str) -> None:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a date")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _allocate_process_report_id() -> int:
    global _NEXT_PROCESS_REPORT_ID
    with _REPORT_ID_LOCK:
        # 以微秒时间戳为基线并保证进程内递增，降低重启后与历史 ID 冲突的概率。
        timestamp_id = max(1, time.time_ns() // 1_000)
        _NEXT_PROCESS_REPORT_ID = max(timestamp_id, _NEXT_PROCESS_REPORT_ID + 1)
        return _NEXT_PROCESS_REPORT_ID
