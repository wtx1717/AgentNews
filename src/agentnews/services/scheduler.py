from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
import inspect
import logging
from typing import Any
from zoneinfo import ZoneInfo

from ..repositories import RepositoryProtocol, ScheduledDigestRun, UserPreferences
from .digest import (
    DigestConfigurationError,
    DigestError,
    DigestIncompleteRunError,
    DigestNoSourcesError,
    DigestPreferences,
    DigestResult,
    DigestStorageError,
)
from .email import EmailService, EmailServiceError


logger = logging.getLogger(__name__)


class DailyDigestScheduler:
    """Application-local daily digest scheduler with persisted task state."""

    def __init__(
        self,
        repository: RepositoryProtocol,
        digest_service_factory: Callable[[], Any],
        email_service: EmailService,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        poll_interval: float = 30.0,
        retry_delays: tuple[float, ...] = (1.0, 5.0),
    ) -> None:
        self.repository = repository
        self.digest_service_factory = digest_service_factory
        self.email_service = email_service
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep or asyncio.sleep
        self.poll_interval = poll_interval
        self.retry_delays = retry_delays
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._started = False
        self._startup_preferences: tuple[object, ...] | None = None
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._active_business_dates: set[date] = set()

    def start(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._stop_event = asyncio.Event()
            self._started = False
            self._startup_preferences = None
            self._task = asyncio.create_task(self.run_forever())
        return self._task

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for active in tuple(self._active_tasks):
            if not active.done():
                active.cancel()
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)

    async def run_forever(self) -> None:
        self._recover_interrupted_runs()
        self._started = True
        try:
            while not self._stop_event.is_set():
                now = self._now()
                preferences = self.repository.get_preferences()
                signature = _schedule_signature(preferences)
                changed = self._startup_preferences is not None and signature != self._startup_preferences
                if self._startup_preferences is None:
                    self._startup_preferences = signature
                    await self._mark_startup_missed(now, preferences)
                elif changed:
                    self._startup_preferences = signature
                    await self._mark_changed_schedule_missed(now, preferences)
                if preferences.daily_send_enabled and _is_due(now, preferences):
                    business_date = _business_date(now, preferences.timezone)
                    if business_date not in self._active_business_dates:
                        active = asyncio.create_task(self.run_due_once(now=now))
                        self._active_tasks.add(active)
                        self._active_business_dates.add(business_date)
                        active.add_done_callback(self._active_tasks.discard)
                        active.add_done_callback(
                            lambda _task, scheduled_date=business_date: self._active_business_dates.discard(scheduled_date)
                        )
                await self._sleep(self.poll_interval)
        finally:
            for active in tuple(self._active_tasks):
                if not active.done():
                    active.cancel()
            if self._active_tasks:
                await asyncio.gather(*self._active_tasks, return_exceptions=True)

    async def run_due_once(self, *, now: datetime | None = None) -> ScheduledDigestRun | None:
        now = now or self._now()
        preferences = self.repository.get_preferences()
        if not preferences.daily_send_enabled or not _is_due(now, preferences):
            return None
        business_date = _business_date(now, preferences.timezone)
        run, _created = self.repository.create_scheduled_run(business_date)
        if run.status in {"succeeded", "failed", "skipped"}:
            return run
        claimed = self.repository.claim_scheduled_run(run.id)
        if claimed is None:
            return self.repository.get_scheduled_run(business_date)
        return await self._execute_run(claimed, preferences)

    async def _execute_run(
        self, run: ScheduledDigestRun, preferences: UserPreferences
    ) -> ScheduledDigestRun:
        current = run
        try:
            while current.attempt_count <= 3:
                try:
                    report_id = current.report_id
                    if report_id is None:
                        result: DigestResult = await self._run_digest(preferences, current.business_date)
                        report_id = result.report_id
                        current = self.repository.update_scheduled_run(
                            current.id, "running", report_id=report_id
                        )
                    email_result = await asyncio.to_thread(
                        self.email_service.send_report,
                        report_id,
                        trigger_source="scheduled_task",
                        idempotency_key=f"scheduled:{current.business_date.isoformat()}",
                        max_smtp_attempts=1,
                        retry_existing=True,
                    )
                    current = self.repository.update_scheduled_run(
                        current.id,
                        "succeeded" if email_result.delivery.status == "sent" else "failed",
                        report_id=report_id,
                        email_delivery_id=email_result.delivery.id,
                        error_code=None if email_result.delivery.status == "sent" else email_result.delivery.error_code,
                    )
                    if current.status == "succeeded":
                        return current
                    if not _is_retryable_error(email_result.delivery.error_code):
                        return current
                    raise _RetryableTaskError(email_result.delivery.error_code or "temporary_error")
                except asyncio.CancelledError:
                    raise
                except (DigestConfigurationError, DigestNoSourcesError) as error:
                    return self.repository.update_scheduled_run(
                        current.id, "failed", error_code=_digest_error_code(error)
                    )
                except EmailServiceError as error:
                    return self.repository.update_scheduled_run(
                        current.id, "failed", error_code="email_error"
                    )
                except (DigestIncompleteRunError, DigestStorageError, _RetryableTaskError, DigestError) as error:
                    code = _digest_error_code(error)
                    if current.attempt_count >= 3 or isinstance(error, DigestStorageError):
                        return self.repository.update_scheduled_run(current.id, "failed", error_code=code)
                    current = self.repository.update_scheduled_run(
                        current.id, "pending", error_code=code
                    )
                    await self._sleep(self.retry_delays[min(current.attempt_count - 1, len(self.retry_delays) - 1)] if self.retry_delays else 0)
                    claimed = self.repository.claim_scheduled_run(current.id)
                    if claimed is None:
                        return self.repository.get_scheduled_run(current.business_date) or current
                    current = claimed
                except Exception:
                    # Unexpected failures are deliberately mapped to one stable code, so the
                    # traceback must be logged or the real cause is lost for good.
                    logger.exception(
                        "计划任务执行出现未预期异常（业务日期 %s，第 %s 次尝试）",
                        current.business_date.isoformat(),
                        current.attempt_count,
                    )
                    if current.attempt_count >= 3:
                        return self.repository.update_scheduled_run(current.id, "failed", error_code="scheduler_error")
                    current = self.repository.update_scheduled_run(current.id, "pending", error_code="scheduler_error")
                    await self._sleep(self.retry_delays[min(current.attempt_count - 1, len(self.retry_delays) - 1)] if self.retry_delays else 0)
                    claimed = self.repository.claim_scheduled_run(current.id)
                    if claimed is None:
                        return self.repository.get_scheduled_run(current.business_date) or current
                    current = claimed
        except asyncio.CancelledError:
            return self.repository.update_scheduled_run(current.id, "failed", error_code="scheduler_stopped")
        return current

    async def _run_digest(self, preferences: UserPreferences, business_date: date) -> DigestResult:
        service = self.digest_service_factory()
        result = service.run_once(
            DigestPreferences(
                topics=preferences.topics,
                keywords=preferences.keywords,
                timezone=preferences.timezone,
            ),
            business_date,
        )
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _mark_startup_missed(self, now: datetime, preferences: UserPreferences) -> None:
        if preferences.daily_send_enabled and _is_after_schedule(now, preferences):
            business_date = _business_date(now, preferences.timezone)
            self.repository.create_scheduled_run(
                business_date, status="skipped", error_code="schedule_missed"
            )

    async def _mark_changed_schedule_missed(self, now: datetime, preferences: UserPreferences) -> None:
        if preferences.daily_send_enabled and _is_after_schedule(now, preferences):
            business_date = _business_date(now, preferences.timezone)
            self.repository.create_scheduled_run(
                business_date, status="skipped", error_code="schedule_missed"
            )

    def _recover_interrupted_runs(self) -> None:
        for run in self.repository.list_scheduled_runs(limit=100000):
            if run.status in {"running", "pending"}:
                self.repository.update_scheduled_run(run.id, "failed", error_code="scheduler_stopped")

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    async def _sleep(self, seconds: float) -> None:
        result = self.sleep(max(0.0, seconds))
        if inspect.isawaitable(result):
            await result


Scheduler = DailyDigestScheduler


class _RetryableTaskError(RuntimeError):
    pass


def _schedule_signature(preferences: UserPreferences) -> tuple[object, ...]:
    return (
        preferences.daily_send_enabled,
        preferences.daily_send_time,
        preferences.timezone,
    )


def _business_date(now: datetime, timezone: str) -> date:
    return now.astimezone(ZoneInfo(timezone)).date()


def _is_due(now: datetime, preferences: UserPreferences) -> bool:
    local = now.astimezone(ZoneInfo(preferences.timezone))
    hour, minute = (int(part) for part in preferences.daily_send_time.split(":"))
    return (local.hour, local.minute, local.second, local.microsecond) >= (hour, minute, 0, 0)


def _is_after_schedule(now: datetime, preferences: UserPreferences) -> bool:
    local = now.astimezone(ZoneInfo(preferences.timezone))
    hour, minute = (int(part) for part in preferences.daily_send_time.split(":"))
    return (local.hour, local.minute, local.second, local.microsecond) > (hour, minute, 0, 0)


def _is_retryable_error(error_code: str | None) -> bool:
    return error_code in {
        "temporary_network_error",
        "smtp_server_error",
        "digest_incomplete",
        "digest_error",
        "scheduler_error",
    }


def _digest_error_code(error: BaseException) -> str:
    if isinstance(error, _RetryableTaskError):
        return str(error)
    if isinstance(error, DigestConfigurationError):
        return "invalid_digest_preferences"
    if isinstance(error, DigestNoSourcesError):
        return "no_sources"
    if isinstance(error, DigestIncompleteRunError):
        return "digest_incomplete"
    if isinstance(error, DigestStorageError):
        return "digest_storage_error"
    if isinstance(error, DigestError):
        return "digest_error"
    return "scheduler_error"

