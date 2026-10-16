from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
import re
import smtplib
import ssl
import time
from typing import Any
from uuid import uuid4

from ..config import Settings, get_settings
from ..repositories import (
    EmailDeliveryRecord,
    EmailDeliverySource,
    RepositoryProtocol,
    ReportStorage,
)


MAX_SMTP_ATTEMPTS = 3
_RETRY_DELAYS = (0.5, 1.0)
_EMAIL_PATTERN = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class EmailSendResult:
    delivery: EmailDeliveryRecord
    duplicate: bool


class EmailServiceError(RuntimeError):
    """Base class for errors that prevent preparing a report email."""


class ReportNotFoundError(EmailServiceError):
    pass


class ReportContentUnavailableError(EmailServiceError):
    pass


class _SMTPFailure(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class EmailService:
    """Send persisted reports and keep an idempotent delivery audit trail."""

    def __init__(
        self,
        repository: RepositoryProtocol,
        report_storage: ReportStorage,
        *,
        settings: Settings | None = None,
        smtp_factory: Callable[..., Any] | None = None,
        smtp_ssl_factory: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.repository = repository
        self.report_storage = report_storage
        self.settings = settings or get_settings()
        self.smtp_factory = smtp_factory or smtplib.SMTP
        self.smtp_ssl_factory = smtp_ssl_factory or (
            smtp_factory if smtp_factory is not None else smtplib.SMTP_SSL
        )
        self.sleeper = sleeper or time.sleep
        self.timeout = timeout

    def send_report(
        self,
        report_id: int | None = None,
        *,
        resend: bool = False,
        trigger_source: EmailDeliverySource = "manual_api",
        idempotency_key: str | None = None,
    ) -> EmailSendResult:
        if report_id is None:
            reports = self.repository.list_reports(limit=1)
            if not reports:
                raise ReportNotFoundError("没有已完成的日报")
            report = reports[0]
        else:
            report = self.repository.get_report(report_id)
            if report is None:
                raise ReportNotFoundError("日报不存在")

        try:
            markdown = self.report_storage.read_report(report.file_path)
        except (OSError, UnicodeError, ValueError):
            raise ReportContentUnavailableError("日报正文不可用，未发送邮件") from None

        recipient = self.repository.get_preferences().email.strip()
        subject = _email_subject(report.title, report.report_date.isoformat())
        body = build_report_email_body(
            title=report.title,
            report_date=report.report_date.isoformat(),
            markdown=markdown,
        )
        key = idempotency_key or _delivery_key(report.id, recipient, resend)
        if resend and idempotency_key is None:
            key = f"{key}:{uuid4()}"

        delivery, created = self.repository.reserve_email_delivery(
            report.id,
            recipient,
            subject,
            trigger_source,
            key,
        )
        if not created:
            return EmailSendResult(delivery=delivery, duplicate=True)

        if not recipient:
            delivery = self.repository.update_email_delivery(
                delivery.id,
                "failed",
                error_code="recipient_not_configured",
                error_message="请先在设置中保存有效的收件邮箱，再明确重发。",
            )
            return EmailSendResult(delivery=delivery, duplicate=False)
        try:
            _validate_email_address(recipient)
        except ValueError:
            delivery = self.repository.update_email_delivery(
                delivery.id,
                "failed",
                error_code="invalid_recipient",
                error_message="设置中的收件邮箱无效，请修正后明确重发。",
            )
            return EmailSendResult(delivery=delivery, duplicate=False)

        try:
            host, port, username, password, sender = self.settings.require_smtp()
            _validate_email_address(sender)
        except ValueError:
            delivery = self.repository.update_email_delivery(
                delivery.id,
                "failed",
                error_code="smtp_not_configured",
                error_message="SMTP 配置不完整或发件地址无效，请配置后明确重发。",
            )
            return EmailSendResult(delivery=delivery, duplicate=False)

        message = _build_message(sender, recipient, subject, body)
        for attempt_number in range(1, MAX_SMTP_ATTEMPTS + 1):
            try:
                self._send_smtp_message(
                    host,
                    port,
                    username,
                    password.get_secret_value(),
                    message,
                    recipient,
                )
            except _SMTPFailure as failure:
                self.repository.record_email_delivery_attempt(
                    delivery.id,
                    attempt_number,
                    "failed",
                    failure.code,
                    failure.message,
                )
                should_retry = failure.retryable and attempt_number < MAX_SMTP_ATTEMPTS
                delivery = self.repository.update_email_delivery(
                    delivery.id,
                    "pending" if should_retry else "failed",
                    error_code=failure.code,
                    error_message=failure.message,
                    retry_count=attempt_number - 1,
                )
                if should_retry:
                    self.sleeper(_RETRY_DELAYS[attempt_number - 1])
                    continue
                return EmailSendResult(delivery=delivery, duplicate=False)
            else:
                self.repository.record_email_delivery_attempt(
                    delivery.id, attempt_number, "sent"
                )
                delivery = self.repository.update_email_delivery(
                    delivery.id,
                    "sent",
                    retry_count=attempt_number - 1,
                )
                return EmailSendResult(delivery=delivery, duplicate=False)

        raise AssertionError("SMTP retry loop ended without a result")

    def _send_smtp_message(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        message: EmailMessage,
        recipient: str,
    ) -> None:
        client = None
        try:
            if self.settings.smtp_use_ssl:
                client = self.smtp_ssl_factory(
                    host,
                    port,
                    timeout=self.timeout,
                    context=ssl.create_default_context(),
                )
            else:
                client = self.smtp_factory(host, port, timeout=self.timeout)
                client.ehlo()
                client.starttls(context=ssl.create_default_context())
            client.ehlo()
            client.login(username, password)
            refused = client.send_message(message, to_addrs=[recipient])
            if refused:
                code = next(iter(refused.values()))[0]
                raise _classify_recipient_refusal(code)
        except _SMTPFailure:
            raise
        except smtplib.SMTPAuthenticationError:
            raise _SMTPFailure(
                "smtp_authentication_failed",
                "SMTP 认证失败，请检查账号和授权码。",
                retryable=False,
            ) from None
        except smtplib.SMTPRecipientsRefused as error:
            code = next(iter(error.recipients.values()))[0] if error.recipients else 550
            raise _classify_recipient_refusal(code) from None
        except smtplib.SMTPResponseException as error:
            raise _classify_response_error(error.smtp_code) from None
        except (OSError, TimeoutError, smtplib.SMTPServerDisconnected):
            raise _SMTPFailure(
                "temporary_network_error",
                "SMTP 网络暂时不可用，系统已按策略重试。",
                retryable=True,
            ) from None
        except smtplib.SMTPException:
            raise _SMTPFailure(
                "smtp_error",
                "SMTP 服务拒绝了邮件请求。",
                retryable=False,
            ) from None
        finally:
            if client is not None:
                try:
                    client.quit()
                except Exception:
                    try:
                        client.close()
                    except Exception:
                        pass


def build_report_email_body(*, title: str, report_date: str, markdown: str) -> str:
    """Render an email exclusively from a completed, persisted report."""
    return f"{title}\n日期：{report_date}\n\n{markdown.rstrip()}\n"


def classify_smtp_error(error: BaseException) -> tuple[str, str, bool]:
    """Return a stable, secret-free error code, message and retry policy."""
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return (
            "smtp_authentication_failed",
            "SMTP 认证失败，请检查账号和授权码。",
            False,
        )
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        failure = _classify_recipient_refusal(
            next(iter(error.recipients.values()))[0] if error.recipients else 550
        )
        return failure.code, failure.message, failure.retryable
    if isinstance(error, smtplib.SMTPResponseException):
        failure = _classify_response_error(error.smtp_code)
        return failure.code, failure.message, failure.retryable
    if isinstance(error, (OSError, TimeoutError, smtplib.SMTPServerDisconnected)):
        return (
            "temporary_network_error",
            "SMTP 网络暂时不可用，系统已按策略重试。",
            True,
        )
    return "smtp_error", "SMTP 服务拒绝了邮件请求。", False


def _classify_recipient_refusal(code: int) -> _SMTPFailure:
    if 400 <= code < 500:
        return _SMTPFailure(
            "temporary_network_error",
            "SMTP 暂时拒收邮件，系统已按策略重试。",
            retryable=True,
        )
    return _SMTPFailure(
        "invalid_recipient",
        "SMTP 服务拒绝了收件地址，请核对后明确重发。",
        retryable=False,
    )


def _classify_response_error(code: int) -> _SMTPFailure:
    if 400 <= code < 500:
        return _SMTPFailure(
            "temporary_network_error",
            "SMTP 暂时不可用，系统已按策略重试。",
            retryable=True,
        )
    return _SMTPFailure(
        "smtp_rejected",
        "SMTP 服务拒绝了邮件请求。",
        retryable=False,
    )


def _validate_email_address(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > 254
        or value.count("@") != 1
        or "\r" in value
        or "\n" in value
    ):
        raise ValueError("invalid email address")
    local, domain = value.rsplit("@", 1)
    if (
        not local
        or len(local) > 64
        or ".." in local
        or ".." in domain
        or not _EMAIL_PATTERN.fullmatch(value)
    ):
        raise ValueError("invalid email address")


def _email_subject(title: str, report_date: str) -> str:
    clean_title = " ".join(title.split())[:180]
    return f"AgentNews 日报 | {report_date} | {clean_title}"


def _delivery_key(report_id: int, recipient: str, resend: bool) -> str:
    normalized_recipient = recipient.strip().casefold()
    suffix = f"resend:{uuid4()}" if resend else "default"
    return f"report:{report_id}:recipient:{normalized_recipient}:{suffix}"


def _build_message(
    sender: str,
    recipient: str,
    subject: str,
    body: str,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    return message
