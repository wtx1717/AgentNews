from __future__ import annotations

from datetime import date
from email.message import EmailMessage
import ssl

from agentnews.config import Settings
from agentnews.repositories import InMemoryRepository, ReportStorage, UserPreferences
from agentnews.services.email import EmailService


class RecordingSMTP:
    def __init__(self):
        self.ehlo_calls = 0
        self.starttls_calls = 0
        self.logins = []
        self.messages = []

    def ehlo(self):
        self.ehlo_calls += 1
        return 250, b"ok"

    def starttls(self, *, context):
        self.starttls_calls += 1
        return 220, b"ready"

    def login(self, username, password):
        self.logins.append((username, password))
        return 235, b"ok"

    def send_message(self, message: EmailMessage, *, to_addrs):
        self.messages.append((message, to_addrs))
        return {}

    def quit(self):
        return 221, b"bye"

    def close(self):
        return None


class RecordingSMTPFactory:
    def __init__(self):
        self.calls = []
        self.client = RecordingSMTP()

    def __call__(self, host, port, *, timeout, context=None):
        self.calls.append((host, port, timeout, context))
        return self.client


class RecordingSMTPSSLFactory:
    def __init__(self):
        self.calls = []
        self.client = RecordingSMTP()

    def __call__(self, host, port, *, timeout, context):
        self.calls.append((host, port, timeout, context))
        return self.client


def _service(tmp_path, *, use_ssl, inject_ssl_factory=True):
    repository = InMemoryRepository()
    repository.save_preferences(UserPreferences(email="user@example.test"))
    reservation = repository.reserve_report(date(2026, 10, 16), "测试日报")
    storage = ReportStorage(tmp_path / "reports")
    stored = storage.save_report(
        reservation.id,
        reservation.report_date,
        reservation.title,
        "# 今日新闻\n",
    )
    report = repository.finalize_report(
        reservation.id,
        stored.relative_path,
        "今日新闻",
        stored.content_hash,
    )
    settings = Settings(
        _env_file=None,
        smtp_host="smtp.example.test",
        smtp_port=465 if use_ssl else 587,
        smtp_use_ssl=use_ssl,
        smtp_user="agentnews",
        smtp_password="test-secret",
        smtp_from="reports@example.test",
    )
    plain_factory = RecordingSMTPFactory()
    ssl_factory = RecordingSMTPSSLFactory()
    service_options = {
        "settings": settings,
        "smtp_factory": plain_factory,
        "sleeper": lambda _delay: None,
    }
    if inject_ssl_factory:
        service_options["smtp_ssl_factory"] = ssl_factory
    service = EmailService(repository, storage, **service_options)
    return service, report, plain_factory, ssl_factory


def test_implicit_tls_uses_smtp_ssl_without_starttls(tmp_path):
    service, report, plain_factory, ssl_factory = _service(tmp_path, use_ssl=True)

    result = service.send_report(report.id)

    assert result.delivery.status == "sent"
    assert plain_factory.calls == []
    assert len(ssl_factory.calls) == 1
    host, port, timeout, context = ssl_factory.calls[0]
    assert (host, port, timeout) == ("smtp.example.test", 465, 10.0)
    assert isinstance(context, ssl.SSLContext)
    assert ssl_factory.client.ehlo_calls == 1
    assert ssl_factory.client.starttls_calls == 0
    assert ssl_factory.client.logins == [("agentnews", "test-secret")]
    assert ssl_factory.client.messages[0][1] == ["user@example.test"]


def test_starttls_remains_the_default_transport(tmp_path):
    service, report, plain_factory, ssl_factory = _service(tmp_path, use_ssl=False)

    result = service.send_report(report.id)

    assert result.delivery.status == "sent"
    assert plain_factory.calls == [("smtp.example.test", 587, 10.0, None)]
    assert ssl_factory.calls == []
    assert plain_factory.client.ehlo_calls == 2
    assert plain_factory.client.starttls_calls == 1


def test_injected_smtp_factory_also_mocks_implicit_tls(tmp_path):
    service, report, plain_factory, ssl_factory = _service(
        tmp_path,
        use_ssl=True,
        inject_ssl_factory=False,
    )

    result = service.send_report(report.id)

    assert result.delivery.status == "sent"
    assert ssl_factory.calls == []
    assert len(plain_factory.calls) == 1
    host, port, timeout, context = plain_factory.calls[0]
    assert (host, port, timeout) == ("smtp.example.test", 465, 10.0)
    assert isinstance(context, ssl.SSLContext)


def test_smtp_use_ssl_defaults_to_true_and_can_be_disabled(monkeypatch):
    assert Settings(_env_file=None).smtp_port == 465
    assert Settings(_env_file=None).smtp_use_ssl is True

    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USE_SSL", "false")

    settings = Settings(_env_file=None)
    assert settings.smtp_port == 587
    assert settings.smtp_use_ssl is False
