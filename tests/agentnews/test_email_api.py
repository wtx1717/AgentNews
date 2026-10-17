from __future__ import annotations

from datetime import date
from email.message import EmailMessage

from fastapi.testclient import TestClient

from agentnews.api import create_app
from agentnews.config import Settings
from agentnews.repositories import InMemoryRepository, ReportStorage, UserPreferences
from agentnews.services.email import EmailService


class FakeSMTP:
    def __init__(self, owner):
        self.owner = owner

    def ehlo(self):
        return 250, b"ok"

    def starttls(self, *, context):
        return 220, b"ready"

    def login(self, username, password):
        return 235, b"ok"

    def send_message(self, message: EmailMessage, *, to_addrs):
        self.owner.messages.append(message)
        return {}

    def quit(self):
        return 221, b"bye"

    def close(self):
        return None


class FakeSMTPFactory:
    def __init__(self):
        self.messages = []
        self.connections = 0

    def __call__(self, *_args, **_kwargs):
        self.connections += 1
        return FakeSMTP(self)


def test_email_api_sends_idempotently_and_exposes_delivery_state(tmp_path):
    repository = InMemoryRepository(report_id_factory=lambda: 1)
    repository.save_preferences(UserPreferences(email="user@example.test"))
    reservation = repository.reserve_report(date(2026, 10, 16), "API 日报")
    storage = ReportStorage(tmp_path / "reports")
    stored = storage.save_report(
        reservation.id,
        reservation.report_date,
        reservation.title,
        "# API 日报\n\n持久化正文。\n",
    )
    repository.finalize_report(
        reservation.id,
        stored.relative_path,
        "API 摘要",
        stored.content_hash,
    )
    settings = Settings(
        _env_file=None,
        smtp_host="smtp.example.test",
        smtp_user="agentnews",
        smtp_password="test-secret",
        smtp_from="reports@example.test",
        report_root=tmp_path / "reports",
        source_archive_root=tmp_path / "sources",
        sqlite_path=tmp_path / "agentnews.sqlite3",
    )
    smtp = FakeSMTPFactory()
    service = EmailService(
        repository,
        storage,
        settings=settings,
        smtp_factory=smtp,
        sleeper=lambda _delay: None,
    )
    client = TestClient(create_app(repository, settings=settings, email_service=service))

    first = client.post("/api/reports/1/email", json={"resend": False})
    invalid = client.post("/api/reports/1/email", json={"resend": 1})
    duplicate = client.post("/api/reports/1/email", json={"resend": False})
    deliveries = client.get("/api/reports/1/email-deliveries")
    report = client.get("/api/reports/1")

    assert first.status_code == 200
    assert first.json()["delivery"]["status"] == "sent"
    assert invalid.status_code == 422
    assert duplicate.json()["duplicate"] is True
    assert smtp.connections == 1
    assert deliveries.json()["items"][0]["attempts"][0]["status"] == "sent"
    assert report.json()["email_deliveries"][0]["status"] == "sent"
