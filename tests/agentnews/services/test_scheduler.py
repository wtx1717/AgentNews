import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace

from agentnews.repositories import InMemoryRepository, UserPreferences
from agentnews.services.digest import DigestResult
from agentnews.services.scheduler import DailyDigestScheduler


class _Digest:
    def __init__(self):
        self.calls = []

    async def run_once(self, preferences, business_date):
        self.calls.append((preferences, business_date))
        return DigestResult(7, "report.md", (), 1, "日报")


class _Email:
    def __init__(self, statuses=("sent",)):
        self.statuses = list(statuses)
        self.calls = []

    def send_report(self, report_id, **kwargs):
        self.calls.append((report_id, kwargs))
        status = self.statuses.pop(0) if self.statuses else "sent"
        return SimpleNamespace(
            delivery=SimpleNamespace(
                id=11,
                status=status,
                error_code="temporary_network_error" if status != "sent" else None,
            )
        )


def _run(coro):
    return asyncio.run(coro)


def test_due_run_uses_user_timezone_and_is_idempotent():
    repository = InMemoryRepository()
    repository.save_preferences(
        UserPreferences(
            timezone="America/Los_Angeles",
            topics=["AI"],
            daily_send_enabled=True,
            daily_send_time="08:00",
        )
    )
    digest = _Digest()
    email = _Email()
    scheduler = DailyDigestScheduler(repository, lambda: digest, email, retry_delays=())
    now = datetime(2026, 10, 16, 15, 0, tzinfo=UTC)

    first = _run(scheduler.run_due_once(now=now))
    second = _run(scheduler.run_due_once(now=now))

    assert first is not None and first.status == "succeeded"
    assert second == first
    assert first.business_date == date(2026, 10, 16)
    assert len(digest.calls) == 1
    assert len(email.calls) == 1
    assert email.calls[0][1]["max_smtp_attempts"] == 1


def test_startup_after_schedule_records_missed_without_running_digest():
    repository = InMemoryRepository()
    repository.save_preferences(
        UserPreferences(
            timezone="Asia/Shanghai",
            topics=["AI"],
            daily_send_enabled=True,
            daily_send_time="08:00",
        )
    )
    digest = _Digest()
    scheduler = DailyDigestScheduler(repository, lambda: digest, _Email())

    _run(
        scheduler._mark_startup_missed(
            datetime(2026, 10, 16, 23, 59, tzinfo=UTC), repository.get_preferences()
        )
    )
    assert repository.list_scheduled_runs() == ()

    _run(
        scheduler._mark_startup_missed(
            datetime(2026, 10, 16, 1, 0, 1, tzinfo=UTC), repository.get_preferences()
        )
    )
    run = repository.get_scheduled_run(date(2026, 10, 16))
    assert run is not None and run.status == "skipped"
    assert run.error_code == "schedule_missed"
    assert digest.calls == []


def test_temporary_email_failure_retries_task_and_reuses_idempotency_key():
    repository = InMemoryRepository()
    repository.save_preferences(
        UserPreferences(topics=["AI"], email="user@example.test", daily_send_enabled=True)
    )
    digest = _Digest()
    email = _Email(("failed", "sent"))
    scheduler = DailyDigestScheduler(repository, lambda: digest, email, retry_delays=())

    result = _run(
        scheduler.run_due_once(now=datetime(2026, 10, 16, 0, 0, tzinfo=UTC))
    )

    assert result is not None and result.status == "succeeded"
    assert result.attempt_count == 2
    assert len(digest.calls) == 1
    assert len(email.calls) == 2
    assert email.calls[0][1]["idempotency_key"] == email.calls[1][1]["idempotency_key"]

