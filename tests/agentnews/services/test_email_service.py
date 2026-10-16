from __future__ import annotations

from datetime import date
from email.message import EmailMessage
import smtplib

from agentnews.config import Settings
from agentnews.repositories import (
    InMemoryRepository,
    ReportStorage,
    SQLiteRepository,
    UserPreferences,
)
from agentnews.services.email import EmailService


class RecordingSMTP:
    def __init__(self, owner, failure=None, failure_stage=None):
        self.owner = owner
        self.failure = failure
        self.failure_stage = failure_stage

    def ehlo(self):
        return 250, b"ok"

    def starttls(self, *, context):
        return 220, b"ready"

    def login(self, username, password):
        self.owner.logins.append((username, password))
        if self.failure_stage == "login":
            raise self.failure
        return 235, b"ok"

    def send_message(self, message: EmailMessage, *, to_addrs):
        self.owner.messages.append((message, to_addrs))
        if self.failure_stage == "send":
            raise self.failure
        return {}

    def quit(self):
        return 221, b"bye"

    def close(self):
        return None


class RecordingSMTPFactory:
    def __init__(self, *, failure=None, failure_stage=None):
        self.failure = failure
        self.failure_stage = failure_stage
        self.messages = []
        self.logins = []
        self.connections = 0

    def __call__(self, host, port, *, timeout):
        self.connections += 1
        if self.failure_stage == "connect":
            raise self.failure
        return RecordingSMTP(self, self.failure, self.failure_stage)


def _settings(*, smtp=True):
    values = {"_env_file": None}
    if smtp:
        values.update(
            smtp_host="smtp.example.test",
            smtp_port=587,
            smtp_user="agentnews",
            smtp_password="test-secret",
            smtp_from="reports@example.test",
        )
    return Settings(**values)


def _report(tmp_path, recipient="user@example.test"):
    repository = InMemoryRepository(report_id_factory=lambda: 1)
    repository.save_preferences(UserPreferences(email=recipient))
    reservation = repository.reserve_report(date(2026, 10, 16), "测试日报")
    storage = ReportStorage(tmp_path / "reports")
    stored = storage.save_report(
        reservation.id,
        reservation.report_date,
        reservation.title,
        "# 今日新闻\n\n仅来自已保存的报告正文。\n",
    )
    report = repository.finalize_report(
        reservation.id,
        stored.relative_path,
        "今日新闻",
        stored.content_hash,
    )
    return repository, storage, report


def test_success_duplicate_idempotency_and_explicit_resend(tmp_path):
    repository, storage, report = _report(tmp_path)
    smtp = RecordingSMTPFactory()
    service = EmailService(
        repository,
        storage,
        settings=_settings(),
        smtp_factory=smtp,
        sleeper=lambda _delay: None,
    )

    first = service.send_report(report.id)
    duplicate = service.send_report(report.id)
    resent = service.send_report(report.id, resend=True)

    assert first.delivery.status == "sent"
    assert first.duplicate is False
    assert duplicate.delivery.id == first.delivery.id
    assert duplicate.duplicate is True
    assert resent.delivery.id != first.delivery.id
    assert resent.delivery.status == "sent"
    assert smtp.connections == 2
    assert len(repository.list_email_deliveries(report.id)) == 2
    message, recipients = smtp.messages[0]
    assert recipients == ["user@example.test"]
    assert message["Subject"] == "AgentNews 日报 | 2026-10-16 | 测试日报"
    assert "仅来自已保存的报告正文" in message.get_content()
    assert smtp.logins == [("agentnews", "test-secret"), ("agentnews", "test-secret")]
    assert repository.list_email_delivery_attempts(first.delivery.id)[0].status == "sent"


def test_missing_smtp_configuration_is_recorded_without_network_access(tmp_path):
    repository, storage, report = _report(tmp_path)
    smtp = RecordingSMTPFactory()
    result = EmailService(
        repository,
        storage,
        settings=_settings(smtp=False),
        smtp_factory=smtp,
    ).send_report(report.id)

    assert result.delivery.status == "failed"
    assert result.delivery.error_code == "smtp_not_configured"
    assert result.delivery.retry_count == 0
    assert smtp.connections == 0
    assert repository.list_email_delivery_attempts(result.delivery.id) == ()


def test_authentication_failure_is_not_retried_and_is_sanitized(tmp_path):
    repository, storage, report = _report(tmp_path)
    smtp = RecordingSMTPFactory(
        failure=smtplib.SMTPAuthenticationError(535, b"credential-value"),
        failure_stage="login",
    )
    result = EmailService(
        repository,
        storage,
        settings=_settings(),
        smtp_factory=smtp,
        sleeper=lambda _delay: None,
    ).send_report(report.id)

    assert result.delivery.status == "failed"
    assert result.delivery.error_code == "smtp_authentication_failed"
    assert result.delivery.retry_count == 0
    assert smtp.connections == 1
    assert len(repository.list_email_delivery_attempts(result.delivery.id)) == 1
    assert "credential-value" not in result.delivery.error_message


def test_temporary_network_errors_retry_three_total_attempts(tmp_path):
    repository, storage, report = _report(tmp_path)
    delays = []

    def unavailable(*_args, **_kwargs):
        raise TimeoutError("private host detail")

    result = EmailService(
        repository,
        storage,
        settings=_settings(),
        smtp_factory=unavailable,
        sleeper=delays.append,
    ).send_report(report.id)

    attempts = repository.list_email_delivery_attempts(result.delivery.id)
    assert result.delivery.status == "failed"
    assert result.delivery.error_code == "temporary_network_error"
    assert result.delivery.retry_count == 2
    assert len(attempts) == 3
    assert [attempt.error_code for attempt in attempts] == ["temporary_network_error"] * 3
    assert delays == [0.5, 1.0]
    assert "private host detail" not in result.delivery.error_message


def test_invalid_saved_email_is_rejected_before_smtp_connection(tmp_path):
    repository, storage, report = _report(tmp_path, recipient="not an email")
    smtp = RecordingSMTPFactory()
    result = EmailService(
        repository,
        storage,
        settings=_settings(),
        smtp_factory=smtp,
    ).send_report(report.id)

    assert result.delivery.status == "failed"
    assert result.delivery.error_code == "invalid_recipient"
    assert smtp.connections == 0
    assert repository.list_email_delivery_attempts(result.delivery.id) == ()


def test_missing_saved_recipient_is_classified_separately(tmp_path):
    repository, storage, report = _report(tmp_path, recipient="")
    smtp = RecordingSMTPFactory()
    result = EmailService(
        repository,
        storage,
        settings=_settings(),
        smtp_factory=smtp,
    ).send_report(report.id)

    assert result.delivery.status == "failed"
    assert result.delivery.error_code == "recipient_not_configured"
    assert smtp.connections == 0


def test_sqlite_delivery_and_attempt_records_survive_repository_restart(tmp_path):
    repository = SQLiteRepository(tmp_path / "agentnews.sqlite3")
    try:
        repository.save_preferences(UserPreferences(email="user@example.test"))
        reservation = repository.reserve_report(date(2026, 10, 16), "SQLite 日报")
        storage = ReportStorage(tmp_path / "reports")
        stored = storage.save_report(
            reservation.id,
            reservation.report_date,
            reservation.title,
            "持久化日报正文。\n",
        )
        report = repository.finalize_report(
            reservation.id,
            stored.relative_path,
            "摘要",
            stored.content_hash,
        )
        smtp = RecordingSMTPFactory()
        result = EmailService(
            repository,
            storage,
            settings=_settings(),
            smtp_factory=smtp,
            sleeper=lambda _delay: None,
        ).send_report(report.id)
        delivery_id = result.delivery.id
    finally:
        repository.close()

    reopened = SQLiteRepository(tmp_path / "agentnews.sqlite3")
    try:
        delivery = reopened.get_email_delivery(delivery_id)
        assert delivery is not None
        assert delivery.status == "sent"
        attempts = reopened.list_email_delivery_attempts(delivery_id)
        assert len(attempts) == 1
        assert attempts[0].status == "sent"
    finally:
        reopened.close()
